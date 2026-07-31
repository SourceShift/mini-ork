"""Daemon-gated tests for the docker ``Workspace`` backend (sandbox P2, Piece 1).

Registration and error-path tests run everywhere (no daemon needed). The
round-trip and timeout tests require a reachable docker daemon and a small test
image, and skip cleanly when either is absent so the default gate stays green on
hosts without docker.

colima caveat (verified 2026-07-31): the macOS default TMPDIR (``/var/folders``)
is NOT shared into the colima VM, so a bind-mount of pytest's ``tmp_path`` is
visible *inside* the container but not on the host dir. ``bind_drive_root``
probes for a genuinely bind-visible directory (falling back to ``$HOME``, which
colima does share) and skips if none exists, so the bind-mount assertion is
never a false failure.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from mini_ork.runtime.backends import docker as docker_backend
from mini_ork.runtime.sandbox import Workspace, get_workspace

TEST_IMAGE = os.environ.get("MO_SANDBOX_TEST_IMAGE", "alpine:latest")


def _docker_available() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        r = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.returncode == 0
    except Exception:
        return False


def _image_ready(image: str) -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    if (
        subprocess.run(
            [exe, "image", "inspect", image], capture_output=True, text=True
        ).returncode
        == 0
    ):
        return True
    try:
        return (
            subprocess.run(
                [exe, "pull", image],
                capture_output=True,
                text=True,
                timeout=180,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _bind_visible_dir(base: str) -> str | None:
    """Return a bind-mount-visible temp dir under ``base``, or None.

    Probes the exact capability the epic flags: a container writing to a
    bind-mounted dir must be readable back on the host. Returns None when
    ``base`` is not shared into the docker VM (the colima ``/var/folders`` case).
    """
    exe = shutil.which("docker")
    if not exe:
        return None
    try:
        probe = tempfile.mkdtemp(prefix=".mo-bindprobe-", dir=base)
    except OSError:
        return None
    try:
        r = subprocess.run(
            [
                exe,
                "run",
                "--rm",
                "-v",
                f"{probe}:/probe",
                TEST_IMAGE,
                "sh",
                "-c",
                "echo ok > /probe/sentinel",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode == 0 and os.path.exists(os.path.join(probe, "sentinel")):
            return probe
    except Exception:
        pass
    shutil.rmtree(probe, ignore_errors=True)
    return None


_DOCKER = _docker_available()
requires_docker = pytest.mark.skipif(
    not _DOCKER, reason="docker daemon not available"
)


@pytest.fixture
def bind_drive_root(tmp_path):
    """A docker-bind-visible drive dir; skips if the host shares none."""
    if not _image_ready(TEST_IMAGE):
        pytest.skip(f"test image {TEST_IMAGE} unavailable/unpullable")
    chosen: str | None = None
    for base in (str(tmp_path), os.path.expanduser("~")):
        chosen = _bind_visible_dir(base)
        if chosen:
            break
    if not chosen:
        pytest.skip("no docker-bind-visible directory (colima shares neither tmp nor $HOME)")
    try:
        yield chosen
    finally:
        shutil.rmtree(chosen, ignore_errors=True)


# --- registration + error paths (no daemon required) ----------------------


def test_import_registers_docker_backend(tmp_path):
    ws = get_workspace("docker", image=TEST_IMAGE, drive_root=str(tmp_path))
    assert isinstance(ws, docker_backend.DockerWorkspace)
    assert isinstance(ws, Workspace)  # satisfies the runtime_checkable protocol


def test_factory_missing_image_raises_clear_runtimeerror(monkeypatch, tmp_path):
    monkeypatch.delenv("MO_SANDBOX_IMAGE", raising=False)
    with pytest.raises(RuntimeError, match="requires an image"):
        get_workspace("docker", drive_root=str(tmp_path))


def test_factory_missing_drive_root_raises_clear_runtimeerror(monkeypatch):
    monkeypatch.delenv("MO_SHARED_DRIVE_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="requires a drive_root"):
        get_workspace("docker", image=TEST_IMAGE)


def test_up_without_reachable_daemon_raises_runtimeerror(tmp_path):
    # A bogus docker binary makes the daemon unreachable deterministically,
    # so this proves the clean-RuntimeError path without needing docker absent.
    ws = docker_backend.DockerWorkspace(
        image=TEST_IMAGE,
        drive_root=str(tmp_path),
        docker_bin="mo-nonexistent-docker-xyz",
    )
    with pytest.raises(RuntimeError):
        ws.up()


# --- round-trip + bind-mount proof (requires a live daemon + image) -------


@requires_docker
def test_round_trip_exec_and_bind_mount(bind_drive_root):
    ws = docker_backend.DockerWorkspace(
        image=TEST_IMAGE, drive_root=bind_drive_root, mount_path="/workspace"
    )
    ws.up()
    try:
        # put/get round-trips content through the container.
        put_path = ws.put("hello-from-put")
        assert ws.get(put_path) == "hello-from-put"

        # exec runs with cwd pinned and merges stdout+stderr.
        rc, out = ws.exec("echo hi", cwd="/workspace", timeout=30)
        assert rc == 0
        assert out.strip() == "hi"

        # a file exec writes to the mount is visible on the HOST drive dir
        # — this is the proof the bind mount is real (cross-agent sharing).
        rc, _ = ws.exec(
            "echo shared > /workspace/shared.txt", cwd="/workspace", timeout=30
        )
        assert rc == 0
        host_file = os.path.join(bind_drive_root, "shared.txt")
        assert os.path.exists(host_file)
        with open(host_file, encoding="utf-8") as fh:
            assert fh.read().strip() == "shared"
    finally:
        ws.down()

    # down() force-removed the container.
    exe = shutil.which("docker")
    assert exe is not None  # guaranteed by @requires_docker
    still_there = subprocess.run(
        [exe, "ps", "-aqf", f"name=^{ws._name}$"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert still_there == ""


@requires_docker
def test_exec_timeout_kills_runaway(tmp_path):
    if not _image_ready(TEST_IMAGE):
        pytest.skip(f"test image {TEST_IMAGE} unavailable/unpullable")
    # Timeout only needs up/exec/down — no host bind visibility required, so a
    # plain tmp_path drive is fine even on colima.
    ws = docker_backend.DockerWorkspace(
        image=TEST_IMAGE, drive_root=str(tmp_path), mount_path="/workspace"
    )
    ws.up()
    try:
        rc, out = ws.exec("sleep 999", cwd="/workspace", timeout=2)
        assert rc != 0
        assert "timeout" in out.lower()
    finally:
        ws.down()  # idempotent even after the timeout stop
