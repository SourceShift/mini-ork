"""SDK/server-gated tests for the microvm ``Workspace`` backend (sandbox P3, Piece 1).

Registration and error-path tests run everywhere (no SDK, no backend needed).
The round-trip and timeout tests require the microsandbox Python SDK
(``pip install microsandbox``) plus a bootable backend — the embedded ``local``
one (``microsandbox.install()`` fetched msb+libkrunfw into ``~/.microsandbox/``)
or a remote backend set via ``set_default_backend``; they are gated behind an
explicit ``MO_MICROVM_LIVE=1`` opt-in — the operator asserting "I can boot a
microVM" — because, unlike docker's ``docker info``, there is no cheap probe that
confirms a bootable microVM without actually booting one. When the opt-in is
unset the live tests skip and the default gate stays green on hosts that can't
boot a microVM.

colima caveat (verified 2026-07-31, docker backend): the macOS default TMPDIR
(``/var/folders``) is not shared into a colima VM. microsandbox's own VM shares
whatever ``Volume.bind`` is given, so the live bind-mount test writes under
``$HOME`` (broadly shared) and falls back to ``tmp_path``.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

from mini_ork.runtime.backends import microvm as microvm_backend
from mini_ork.runtime.sandbox import Workspace, get_workspace

TEST_IMAGE = os.environ.get("MO_SANDBOX_TEST_IMAGE", "alpine:latest")


def _sdk_installed() -> bool:
    return importlib.util.find_spec("microsandbox") is not None


def _microvm_live() -> bool:
    """Operator opt-in: SDK importable AND a bootable backend they assert works."""
    return _sdk_installed() and bool(
        (os.environ.get("MO_MICROVM_LIVE") or "").strip()
    )


requires_microvm = pytest.mark.skipif(
    not _microvm_live(),
    reason="microVM live tests need the microsandbox SDK + a bootable backend "
    "(local install or remote); set MO_MICROVM_LIVE=1 to enable",
)


@pytest.fixture
def bind_drive_root(tmp_path):
    """A drive dir the microVM can bind-mount; prefer $HOME (broadly shared)."""
    base = os.path.expanduser("~")
    root = os.path.join(base, ".mo-microvm-test")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        root = str(tmp_path)
    try:
        yield root
    finally:
        # Best-effort cleanup of only what the test wrote (never $HOME itself).
        for name in ("shared.txt",):
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass


# --- registration + error paths (no SDK, no server required) --------------


def test_import_registers_microvm_backend(tmp_path):
    ws = get_workspace("microvm", image=TEST_IMAGE, drive_root=str(tmp_path))
    assert isinstance(ws, microvm_backend.MicrovmWorkspace)
    assert isinstance(ws, Workspace)  # satisfies the runtime_checkable protocol


def test_factory_missing_image_raises_clear_runtimeerror(monkeypatch, tmp_path):
    monkeypatch.delenv("MO_SANDBOX_IMAGE", raising=False)
    with pytest.raises(RuntimeError, match="requires an image"):
        get_workspace("microvm", drive_root=str(tmp_path))


def test_factory_missing_drive_root_raises_clear_runtimeerror(monkeypatch):
    monkeypatch.delenv("MO_SHARED_DRIVE_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="requires a drive_root"):
        get_workspace("microvm", image=TEST_IMAGE)


def test_ctor_rejects_empty_image_and_drive(tmp_path):
    with pytest.raises(ValueError, match="non-empty image"):
        microvm_backend.MicrovmWorkspace(image="", drive_root=str(tmp_path))
    with pytest.raises(ValueError, match="non-empty drive_root"):
        microvm_backend.MicrovmWorkspace(image=TEST_IMAGE, drive_root="")


def test_int_env_parses_or_none(monkeypatch):
    monkeypatch.setenv("MO_TEST_INT", "4")
    assert microvm_backend._int_env("MO_TEST_INT") == 4
    monkeypatch.setenv("MO_TEST_INT", "  ")
    assert microvm_backend._int_env("MO_TEST_INT") is None
    monkeypatch.setenv("MO_TEST_INT", "notanint")
    assert microvm_backend._int_env("MO_TEST_INT") is None
    monkeypatch.delenv("MO_TEST_INT", raising=False)
    assert microvm_backend._int_env("MO_TEST_INT") is None


def test_exec_before_up_raises_runtimeerror(tmp_path):
    ws = microvm_backend.MicrovmWorkspace(
        image=TEST_IMAGE, drive_root=str(tmp_path)
    )
    with pytest.raises(RuntimeError, match="before up"):
        ws.exec("echo hi", cwd="/workspace", timeout=5)


def test_up_without_backend_raises_runtimeerror(monkeypatch, tmp_path):
    # Loud-failure contract: when the microsandbox backend is unavailable, up()
    # must raise a clear RuntimeError with the remedy — never a silent host
    # fallback. "Unavailable" has two shapes: the SDK isn't importable, or it is
    # but the embedded local backend hasn't been installed (no msb+libkrunfw in
    # ~/.microsandbox/). Unlike docker's out-of-process daemon, the local backend
    # runs in-process, so once install() has run there is no "server down" state
    # to assert — the only gate is is_installed(). We force that gate False (when
    # the SDK is present) so the outcome is deterministic regardless of whether
    # THIS host happens to have run install(); otherwise the test would flake
    # green on a fresh box and red on a developer box that booted a microVM once.
    if _sdk_installed():
        import microsandbox

        monkeypatch.setattr(microsandbox, "is_installed", lambda: False)
    ws = microvm_backend.MicrovmWorkspace(
        image=TEST_IMAGE, drive_root=str(tmp_path)
    )
    with pytest.raises(RuntimeError):
        ws.up()


# --- round-trip + bind-mount proof (requires SDK + a live msb server) ------


@requires_microvm
def test_round_trip_exec_and_bind_mount(bind_drive_root):
    ws = microvm_backend.MicrovmWorkspace(
        image=TEST_IMAGE, drive_root=bind_drive_root, mount_path="/workspace"
    )
    try:
        ws.up()
    except RuntimeError as exc:  # opt-in set but backend can't actually boot
        pytest.skip(f"no bootable microVM backend: {exc}")
    try:
        # put/get round-trips content through the microVM.
        put_path = ws.put("hello-from-put")
        assert ws.get(put_path) == "hello-from-put"

        # exec runs with cwd pinned and merges stdout+stderr.
        rc, out = ws.exec("echo hi", cwd="/workspace", timeout=30)
        assert rc == 0
        assert out.strip() == "hi"

        # a file exec writes to the mount is visible on the HOST drive dir
        # — the proof the bind mount is real (cross-agent sharing).
        rc, _ = ws.exec(
            "echo shared > /workspace/shared.txt", cwd="/workspace", timeout=30
        )
        assert rc == 0
        host_file = os.path.join(bind_drive_root, "shared.txt")
        assert os.path.exists(host_file)
        with open(host_file, encoding="utf-8") as fh:
            assert fh.read().strip() == "shared"
    finally:
        ws.down()  # cattle: stops + removes the microVM, leaves the drive


@requires_microvm
def test_exec_timeout_kills_runaway(tmp_path):
    ws = microvm_backend.MicrovmWorkspace(
        image=TEST_IMAGE, drive_root=str(tmp_path), mount_path="/workspace"
    )
    try:
        ws.up()
    except RuntimeError as exc:
        pytest.skip(f"no bootable microVM backend: {exc}")
    try:
        rc, out = ws.exec("sleep 999", cwd="/workspace", timeout=2)
        assert rc != 0
        assert "timeout" in out.lower()
    finally:
        ws.down()
