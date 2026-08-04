"""Tests for ``MicrovmWorkspace.spawn`` — the scope=agent CLI-in-microVM
transport (SE-3 Increment 3).

Two layers, matching ``test_microvm_workspace.py``:

* **Daemon-free** — a fake ``sb`` (an ``async def exec`` that records its call)
  driven through the REAL async→sync bridge (a live per-instance event loop, not
  a mocked ``_run``) proves the SDK-call shape, the two SDK-divergence
  normalizations (timeout→124, missing-binary→127), and the env allowlist. These
  run everywhere — no SDK, no microVM.
* **Live** — a bootable microVM proves the real round-trip (stdin→stdout, stderr
  kept separate, faithful rc, allowlisted env forwarded while host PATH is NOT),
  a missing CLI returns ``rc=127``, and a runaway is reaped on timeout with
  ``rc=124``. Gated behind ``MO_MICROVM_LIVE=1`` (the operator asserting "I can
  boot a microVM"); skips cleanly otherwise.

The env boundary is an allowlist by design (host ``os.environ`` is untrusted at
the sandbox edge): see docs/decisions/20260804-docker-spawn-env-injection.md.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os

import pytest

from mini_ork.runtime.backends import microvm as microvm_backend
from mini_ork.runtime.backends.microvm import MicrovmWorkspace

TEST_IMAGE = os.environ.get("MO_SANDBOX_TEST_IMAGE", "alpine:latest")


# --- fakes: the microsandbox SDK seam, no daemon --------------------------


class _FakeExecOutput:
    """Stand-in for the SDK's ``ExecOutput`` — separate streams + an exit code."""

    def __init__(self, exit_code: int | None = 0, stdout_text="", stderr_text=""):
        self.exit_code = exit_code
        self.stdout_text = stdout_text
        self.stderr_text = stderr_text


class _FakeSandbox:
    """A fake ``Sandbox`` whose ``exec`` is a coroutine (as the real pyo3-async
    SDK's is), so it slots into ``MicrovmWorkspace._run`` and exercises the real
    ``run_until_complete`` bridge. Records the one call for argv/shape assertions;
    either returns a fixed ``ExecOutput`` or raises a fixed exception."""

    def __init__(self, *, out=None, raises=None):
        self._out = out if out is not None else _FakeExecOutput()
        self._raises = raises
        self.calls: list[dict] = []

    async def exec(self, cmd, args=None, **kwargs):
        self.calls.append({"cmd": cmd, "args": args, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._out


@pytest.fixture
def make_ws(tmp_path):
    """Factory: a MicrovmWorkspace wired to a fake ``sb`` and a REAL event loop
    (so ``_run`` drives the actual async bridge). Closes every loop it created."""
    created: list[asyncio.AbstractEventLoop] = []

    def _make(sb) -> MicrovmWorkspace:
        ws = MicrovmWorkspace(image=TEST_IMAGE, drive_root=str(tmp_path))
        ws._sb = sb
        ws._loop = asyncio.new_event_loop()
        created.append(ws._loop)
        return ws

    try:
        yield _make
    finally:
        for loop in created:
            if not loop.is_closed():
                loop.close()


# --- guards ----------------------------------------------------------------


def test_spawn_before_up_raises(tmp_path):
    ws = MicrovmWorkspace(image=TEST_IMAGE, drive_root=str(tmp_path))
    with pytest.raises(RuntimeError, match="before up"):
        ws.spawn(["cli"], stdin="", timeout=5, env={}, cwd=None)


def test_spawn_empty_argv_raises(make_ws):
    ws = make_ws(_FakeSandbox())
    with pytest.raises(ValueError, match="non-empty argv"):
        ws.spawn([], stdin="", timeout=5, env={}, cwd=None)


# --- SDK-call shape (argv-direct, stdin bytes, tty=False, allowlist) --------


def test_spawn_calls_exec_argv_direct_stdin_bytes_no_tty(make_ws):
    sb = _FakeSandbox(out=_FakeExecOutput(exit_code=0, stdout_text="OUT", stderr_text="ERR"))
    ws = make_ws(sb)

    rc, out, err = ws.spawn(
        ["mycli", "--flag"],
        stdin="PROMPT",
        timeout=30,
        env={"OPENAI_API_KEY": "k", "PATH": "/leak"},
        cwd="/work",
    )

    assert (rc, out, err) == (0, "OUT", "ERR")
    call = sb.calls[0]
    assert call["cmd"] == "mycli"  # argv[0]
    assert call["args"] == ["--flag"]  # argv[1:], run directly (no shell)
    assert call["cwd"] == "/work"  # cwd pinned
    assert call["stdin"] == b"PROMPT"  # bytes = payload (str stdin is a MODE)
    assert call["tty"] is False  # A.1: no pseudo-TTY → nothing opens /dev/tty
    assert call["timeout"] == 30.0
    # allowlist applied at the boundary: provider key crosses, host PATH dropped.
    assert call["env"] == {"OPENAI_API_KEY": "k"}


def test_spawn_default_workdir_is_mount_path(make_ws):
    sb = _FakeSandbox()
    ws = make_ws(sb)  # default mount_path == /workspace
    ws.spawn(["cli"], stdin="", timeout=5, env={}, cwd=None)
    assert sb.calls[0]["cwd"] == "/workspace"


def test_spawn_streams_separated_and_faithful_rc(make_ws):
    sb = _FakeSandbox(
        out=_FakeExecOutput(exit_code=7, stdout_text="the-out", stderr_text="the-err")
    )
    ws = make_ws(sb)
    # ExecOutput keeps stdout/stderr APART (contrast exec's merge) — the whole
    # reason spawn exists: a merged stream would corrupt the provider's JSON.
    assert ws.spawn(["cli"], stdin="", timeout=5, env={}, cwd=None) == (
        7,
        "the-out",
        "the-err",
    )


def test_spawn_none_exit_code_defaults_to_zero(make_ws):
    sb = _FakeSandbox(out=_FakeExecOutput(exit_code=None, stdout_text="x"))
    ws = make_ws(sb)
    rc, out, _ = ws.spawn(["cli"], stdin="", timeout=5, env={}, cwd=None)
    assert (rc, out) == (0, "x")


def test_spawn_applies_env_allowlist(make_ws):
    # The security-axis invariant at the microVM boundary specifically: only the
    # harness + provider keys cross; unrelated host secrets (AWS) and shell
    # identity (HOME) are dropped so injecting into the VM can't leak them.
    sb = _FakeSandbox()
    ws = make_ws(sb)
    ws.spawn(
        ["cli"],
        stdin="",
        timeout=5,
        env={
            "MO_FOO": "1",
            "ANTHROPIC_API_KEY": "k",
            "AWS_SECRET_ACCESS_KEY": "leak",
            "HOME": "/h",
        },
        cwd=None,
    )
    assert sb.calls[0]["env"] == {"MO_FOO": "1", "ANTHROPIC_API_KEY": "k"}


# --- the two SDK-divergence normalizations to the dispatch contract --------


def test_spawn_timeout_returns_124(make_ws):
    # The SDK RAISES a timeout (asyncio.TimeoutError) rather than returning; the
    # transport maps it to rc=124 + empty stdout so dispatch finalizes it exactly
    # like a host timeout.
    sb = _FakeSandbox(raises=asyncio.TimeoutError())
    ws = make_ws(sb)
    rc, out, err = ws.spawn(["sleep", "999"], stdin="", timeout=2, env={}, cwd=None)
    assert rc == 124 and out == "" and "timeout" in err.lower()


def test_spawn_missing_binary_normalizes_to_127(make_ws):
    # The SDK RAISES when the binary can't be spawned (missing CLI) where
    # host/docker RETURN rc=127; the transport normalizes a spawn-START failure
    # to rc=127 rather than leaking an exception past the (rc, out, err) contract.
    ms = pytest.importorskip("microsandbox")
    exc = ms.MicrosandboxError(
        'exec failed: spawn "no-such-cli-xyz": No such file or directory (os error 2)'
    )
    sb = _FakeSandbox(raises=exc)
    ws = make_ws(sb)
    rc, out, err = ws.spawn(["no-such-cli-xyz"], stdin="", timeout=5, env={}, cwd=None)
    assert rc == 127 and out == "" and "spawn failed" in err.lower()


def test_spawn_timeout_worded_microsandbox_error_maps_to_124(make_ws):
    # Belt-and-suspenders: if a build words the timeout as a MicrosandboxError
    # rather than raising the typed ExecTimeoutError, it still maps to rc=124.
    ms = pytest.importorskip("microsandbox")
    exc = ms.MicrosandboxError("exec failed: timeout after 2s")
    sb = _FakeSandbox(raises=exc)
    ws = make_ws(sb)
    rc, out, err = ws.spawn(["cli"], stdin="", timeout=2, env={}, cwd=None)
    assert rc == 124 and out == "" and "timeout" in err.lower()


def test_spawn_non_start_microsandbox_error_propagates(make_ws):
    # A genuine VM/infra fault (not a never-started process, not a timeout) must
    # propagate LOUDLY — a silent rc would mask a broken sandbox as a bad CLI run.
    ms = pytest.importorskip("microsandbox")
    exc = ms.MicrosandboxError("vm crashed: guest kernel panic")
    sb = _FakeSandbox(raises=exc)
    ws = make_ws(sb)
    with pytest.raises(ms.MicrosandboxError):
        ws.spawn(["cli"], stdin="", timeout=5, env={}, cwd=None)


def test_is_spawn_start_failure_wording_canary():
    # Pins the SDK's ENOENT wording that rc=127 detection keys on; a future SDK
    # message change trips this canary BEFORE it silently misroutes a missing-CLI
    # error into a loud propagation (or vice-versa).
    f = microvm_backend._is_spawn_start_failure
    assert f(Exception('exec failed: spawn "foo": No such file or directory (os error 2)'))
    assert f(Exception("No such file or directory"))
    assert not f(Exception("vm boot failed"))
    assert not f(Exception("connection refused"))


# --- live round-trip (requires the SDK + a bootable microVM) ---------------


def _sdk_installed() -> bool:
    return importlib.util.find_spec("microsandbox") is not None


def _microvm_live() -> bool:
    """Operator opt-in: SDK importable AND a bootable backend they assert works."""
    return _sdk_installed() and bool((os.environ.get("MO_MICROVM_LIVE") or "").strip())


requires_microvm = pytest.mark.skipif(
    not _microvm_live(),
    reason="microVM live tests need the microsandbox SDK + a bootable backend "
    "(local install or remote); set MO_MICROVM_LIVE=1 to enable",
)


@pytest.fixture
def bind_drive_root(tmp_path):
    """A drive dir the microVM can bind-mount; prefer $HOME (broadly shared —
    macOS /var/folders is NOT shared into a colima/microsandbox VM)."""
    root = os.path.join(os.path.expanduser("~"), ".mo-microvm-test")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        root = str(tmp_path)
    yield root


@requires_microvm
def test_spawn_live_roundtrip_streams_env_and_rc(bind_drive_root):
    ws = MicrovmWorkspace(
        image=TEST_IMAGE, drive_root=bind_drive_root, mount_path="/workspace"
    )
    try:
        ws.up()
    except RuntimeError as exc:  # opt-in set but backend can't actually boot
        pytest.skip(f"no bootable microVM backend: {exc}")
    try:
        # cat echoes stdin→stdout; a line goes to stderr; exit code is faithful;
        # $MO_PROBE is forwarded (allowlist) but $PATH is the VM's own, NOT the
        # host /bogus we passed (proving host PATH was dropped).
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


@requires_microvm
def test_spawn_live_missing_binary_returns_127(bind_drive_root):
    ws = MicrovmWorkspace(
        image=TEST_IMAGE, drive_root=bind_drive_root, mount_path="/workspace"
    )
    try:
        ws.up()
    except RuntimeError as exc:
        pytest.skip(f"no bootable microVM backend: {exc}")
    try:
        rc, out, _ = ws.spawn(
            ["no-such-cli-xyz"], stdin="", timeout=30, env={}, cwd="/workspace"
        )
        assert rc == 127  # never-started process → dispatch spawn-fail contract
        assert out == ""
    finally:
        ws.down()


@requires_microvm
def test_spawn_live_timeout_returns_124(bind_drive_root):
    ws = MicrovmWorkspace(
        image=TEST_IMAGE, drive_root=bind_drive_root, mount_path="/workspace"
    )
    try:
        ws.up()
    except RuntimeError as exc:
        pytest.skip(f"no bootable microVM backend: {exc}")
    try:
        rc, out, err = ws.spawn(
            ["sleep", "999"], stdin="", timeout=2, env={}, cwd="/workspace"
        )
        assert rc == 124
        assert out == ""
        assert "timeout" in err.lower()
    finally:
        ws.down()  # idempotent even after the timeout
