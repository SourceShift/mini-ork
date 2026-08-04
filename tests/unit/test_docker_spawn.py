"""Tests for ``DockerWorkspace.spawn`` — the scope=agent CLI-in-container
transport (SE-3 Increment 2).

Two layers, matching ``test_docker_workspace.py``:

* **Daemon-free** — the ``docker exec`` argv / stream-shape / timeout contract is
  proven by faking the ``subprocess.run`` seam. These run everywhere. (The pure
  env-allowlist policy is shared with the microvm backend and tested once in
  ``tests/unit/test_workspace_env.py`` — this file only proves docker *applies*
  it, via bare ``-e KEY`` flags whose values are forwarded from the client's own
  env, never the argv — so host ``ps`` can't read a secret.)
* **Daemon-gated** — a live ``alpine`` container proves the real round-trip
  (stdin → stdout, stderr kept separate, faithful rc, allowlisted env forwarded
  while host PATH is NOT), and that a runaway is reaped on timeout with
  ``rc=124``. Skips cleanly with no docker.

The env boundary is an allowlist by design (host ``os.environ`` is untrusted at
the container edge): see docs/decisions/20260804-docker-spawn-env-injection.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from mini_ork.runtime.backends import docker as docker_backend
from mini_ork.runtime.backends.docker import DockerWorkspace

TEST_IMAGE = os.environ.get("MO_SANDBOX_TEST_IMAGE", "alpine:latest")


# --- docker exec argv / stream / timeout contract (faked subprocess) -------


def _ws(tmp_path) -> DockerWorkspace:
    """A DockerWorkspace with a fake live container id and a deterministic exe,
    so argv assertions don't depend on where ``docker`` is installed."""
    ws = DockerWorkspace(image=TEST_IMAGE, drive_root=str(tmp_path))
    ws._cid = "cid123"
    ws._exe = lambda: "docker"  # type: ignore[method-assign]
    return ws


def test_spawn_before_up_raises(tmp_path):
    ws = DockerWorkspace(image=TEST_IMAGE, drive_root=str(tmp_path))
    with pytest.raises(RuntimeError, match="before up"):
        ws.spawn(["cli"], stdin="", timeout=5, env={}, cwd=None)


def test_spawn_builds_exec_argv_no_tty_stdin_on_pipe(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, returncode=0, stdout="OUT", stderr="ERR")

    monkeypatch.setattr(docker_backend.subprocess, "run", fake_run)
    ws = _ws(tmp_path)

    rc, out, err = ws.spawn(
        ["mycli", "--flag"], stdin="PROMPT", timeout=30, env={}, cwd="/work"
    )

    argv = captured["argv"]
    assert argv[0] == "docker"
    assert argv[1] == "exec"
    assert "-i" in argv  # stdin piped
    assert "-t" not in argv  # A.1: NO TTY allocated in the container
    assert argv[argv.index("-w") + 1] == "/work"  # cwd pinned
    # everything after the container id is the argv, run directly (no shell)
    cid_at = argv.index("cid123")
    assert argv[cid_at + 1 :] == ["mycli", "--flag"]
    # prompt rides on stdin (input=), never argv/env
    assert captured["kwargs"]["input"] == "PROMPT"
    # streams SEPARATE (contrast exec's stderr=STDOUT merge)
    assert captured["kwargs"]["stdout"] is subprocess.PIPE
    assert captured["kwargs"]["stderr"] is subprocess.PIPE
    assert captured["kwargs"]["timeout"] == 30
    assert captured["kwargs"]["check"] is False


def test_spawn_default_workdir_is_mount_path(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        docker_backend.subprocess,
        "run",
        lambda argv, **kw: captured.update(argv=argv)
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    ws = _ws(tmp_path)  # default mount_path == /workspace
    ws.spawn(["cli"], stdin="", timeout=5, env={}, cwd=None)
    argv = captured["argv"]
    assert argv[argv.index("-w") + 1] == "/workspace"


def test_spawn_streams_separated_and_faithful_rc(tmp_path, monkeypatch):
    monkeypatch.setattr(
        docker_backend.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 7, "the-out", "the-err"),
    )
    ws = _ws(tmp_path)
    rc, out, err = ws.spawn(["cli"], stdin="", timeout=5, env={}, cwd=None)
    assert (rc, out, err) == (7, "the-out", "the-err")


def test_spawn_forwards_only_allowlisted_env_by_name_not_value(tmp_path, monkeypatch):
    """Allowlisted keys cross as bare ``-e KEY`` (name only, sorted); host-shell +
    unrelated secrets are dropped; the VALUES ride in the child env, never argv."""
    captured: dict = {}
    monkeypatch.setattr(
        docker_backend.subprocess,
        "run",
        lambda argv, **kw: captured.update(argv=argv, kwargs=kw)
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    ws = _ws(tmp_path)
    ws.spawn(
        ["cli"],
        stdin="",
        timeout=5,
        env={
            "OPENAI_API_KEY": "k1",
            "MO_FOO": "k2",
            "PATH": "/leak",
            "AWS_SECRET_ACCESS_KEY": "leak",
        },
        cwd=None,
    )
    argv = captured["argv"]
    # every "-e" carries a bare KEY NAME (no "=VALUE"), sorted; host PATH + AWS drop
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert names == ["MO_FOO", "OPENAI_API_KEY"]
    assert "PATH" not in names and "AWS_SECRET_ACCESS_KEY" not in names
    # the values are forwarded via the child process env (docker reads them there),
    # NOT via argv — /proc/<pid>/environ is owner+root-only, unlike ps-readable argv
    child_env = captured["kwargs"]["env"]
    assert child_env["MO_FOO"] == "k2"
    assert child_env["OPENAI_API_KEY"] == "k1"


def test_spawn_never_puts_a_secret_value_in_argv(tmp_path, monkeypatch):
    """Security regression guard. The whole reason for the bare ``-e KEY`` form is
    that a secret VALUE in argv is world-readable via ``ps aux``; assert no
    allowlisted value string appears anywhere in the argv (only the KEY names do)."""
    captured: dict = {}
    monkeypatch.setattr(
        docker_backend.subprocess,
        "run",
        lambda argv, **kw: captured.update(argv=argv)
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    ws = _ws(tmp_path)
    secret = "sk-super-secret-do-not-leak"
    ws.spawn(
        ["cli"],
        stdin="",
        timeout=5,
        env={"ANTHROPIC_API_KEY": secret, "MO_TOKEN": secret},
        cwd=None,
    )
    argv = captured["argv"]
    assert all(secret not in tok for tok in argv), (
        f"secret VALUE leaked into docker argv (host `ps` exposure): {argv}"
    )
    # the key NAMES are expected in argv (that is safe); only values must be absent
    assert "ANTHROPIC_API_KEY" in argv and "MO_TOKEN" in argv


def test_spawn_timeout_stops_container_and_returns_124(tmp_path, monkeypatch):
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, float(kwargs.get("timeout") or 0))

    monkeypatch.setattr(docker_backend.subprocess, "run", boom)
    ws = _ws(tmp_path)
    stop_calls: list = []
    ws._docker = lambda *a, **k: stop_calls.append((a, k))  # type: ignore[method-assign]

    rc, out, err = ws.spawn(["cli"], stdin="", timeout=2, env={}, cwd=None)

    assert rc == 124
    assert out == ""  # empty stdout so dispatch finalizes like a host timeout
    assert "timeout" in err.lower()
    # the container was stopped (killing the host-side exec client would not reap
    # the in-container process)
    assert stop_calls and stop_calls[0][0][0] == "stop"


# --- live round-trip (requires a docker daemon + image) -------------------


def _docker_available() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return (
            subprocess.run(
                [exe, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=20,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _image_ready(image: str) -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    if subprocess.run([exe, "image", "inspect", image], capture_output=True).returncode == 0:
        return True
    try:
        return subprocess.run(
            [exe, "pull", image], capture_output=True, text=True, timeout=180
        ).returncode == 0
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker daemon not available"
)


@requires_docker
def test_spawn_live_roundtrip_streams_env_and_rc(tmp_path):
    if not _image_ready(TEST_IMAGE):
        pytest.skip(f"test image {TEST_IMAGE} unavailable/unpullable")
    ws = DockerWorkspace(image=TEST_IMAGE, drive_root=str(tmp_path))
    ws.up()
    try:
        # cat echoes stdin→stdout; a line goes to stderr; exit code is faithful;
        # $MO_PROBE is forwarded (allowlist) but $PATH is the CONTAINER's own,
        # NOT the host /bogus we passed (proving host PATH was dropped). MO_PROBE
        # crossing at all proves the bare `-e MO_PROBE` + child-env forwarding
        # works end-to-end (a broken plumbing would leave probe= empty).
        script = "cat; echo to-stderr 1>&2; echo probe=$MO_PROBE; echo path=$PATH; exit 7"
        rc, out, err = ws.spawn(
            ["sh", "-c", script],
            stdin="hello-stdin\n",
            timeout=30,
            env={"MO_PROBE": "xyz", "PATH": "/bogus-host-path"},
            cwd="/workspace",
        )
        assert rc == 7  # faithful exit code
        assert "hello-stdin" in out  # stdin round-tripped to stdout
        assert "probe=xyz" in out  # allowlisted env crossed the boundary
        assert "path=/bogus-host-path" not in out  # host PATH did NOT leak in
        assert "to-stderr" in err  # stderr kept SEPARATE from stdout
        assert "to-stderr" not in out
    finally:
        ws.down()


@requires_docker
def test_spawn_live_timeout_returns_124(tmp_path):
    if not _image_ready(TEST_IMAGE):
        pytest.skip(f"test image {TEST_IMAGE} unavailable/unpullable")
    ws = DockerWorkspace(image=TEST_IMAGE, drive_root=str(tmp_path))
    ws.up()
    try:
        rc, out, err = ws.spawn(["sleep", "999"], stdin="", timeout=2, env={}, cwd="/workspace")
        assert rc == 124
        assert out == ""
        assert "timeout" in err.lower()
    finally:
        ws.down()  # idempotent even after the timeout stop
