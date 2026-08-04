"""Isolation-selector tests — the SE-3 hybrid spawn seam (SC3 / RQ5).

``core.dispatch`` now owns the spawn CONTRACT (argv, prompt-on-stdin, separated
stdout/stderr, timeout→rc=124, spawn-fail→rc=127, telemetry parse) and delegates
the *transport* to a thin per-backend ``Workspace.spawn`` primitive. The
``host`` backend keeps today's in-process ``Popen`` verbatim via
``core.spawn_local`` (zero regression); ``docker``/``microvm`` route the harness
CLI *itself* into a container/microVM (implemented in Increments 2/3, stubbed
here). ``DispatchRequest.workspace`` (default ``"host"``) is the selector.

What is pinned here:

1. **Zero regression on host.** The default is ``"host"``; ``spawn_local``
   separates streams and returns a faithful rc; a host dispatch behaves exactly
   as before.
2. **A.1 invariant lives in ONE place.** ``spawn_local`` itself severs the
   controlling terminal (session leader, no ``/dev/tty``) and reaps the whole
   process group on timeout — so every backend that reuses it inherits the
   incident-Layer-1 fix by construction.
3. **The delegation contract holds.** A non-host workspace is driven
   ``up() → spawn() → down()`` and its ``(rc, stdout, stderr)`` triple flows
   through the SAME finalize as the host path (telemetry parse applied once).
4. **Unbuilt backends fail loudly.** ``docker``/``microvm`` spawn raise
   ``NotImplementedError`` (not a silent host fallback) until Increments 2/3.
5. **The selector precedence is correct** (explicit request > scope=agent > host).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

import pytest

from mini_ork.dispatch.core import dispatch, spawn_local
from mini_ork.dispatch.models import DispatchRequest
from mini_ork.dispatch.providers import (
    _select_workspace,
    claude_result_text,
    claude_session_id,
)
from mini_ork.runtime import sandbox
from mini_ork.runtime.agent_workspace import resolve_spawn_workspace
from mini_ork.runtime.sandbox import (
    LocalWorkspace,
    Workspace,
    register_workspace_backend,
)

# A child that reports whether it is a session leader (controlling terminal
# severed) and whether /dev/tty is reachable — the A.1 Layer-1 probe.
_TTY_PROBE = (
    "import os, sys\n"
    "sid, pid = os.getsid(0), os.getpid()\n"
    "try:\n"
    "    fd = os.open('/dev/tty', os.O_RDONLY); os.close(fd); tty = 'OPEN'\n"
    "except OSError:\n"
    "    tty = 'NOTTY'\n"
    "sys.stdout.write(f'{sid == pid}:{tty}')\n"
)


# ── 1. Zero regression on host ──────────────────────────────────────────────


def test_default_workspace_is_host() -> None:
    # The selector defaults to the in-process path: every existing caller keeps
    # today's behavior without touching the new field.
    assert DispatchRequest(model="m", prompt="").workspace == "host"


def test_spawn_local_separates_streams_and_faithful_rc() -> None:
    prog = "import sys; sys.stdout.write('OUT'); sys.stderr.write('ERR'); sys.exit(3)"
    rc, out, err = spawn_local(
        [sys.executable, "-c", prog],
        stdin="",
        timeout=30,
        env=os.environ,
        cwd=None,
    )
    # exec's merged stream cannot express this — separation is the whole reason
    # dispatch needs spawn, not exec.
    assert (rc, out, err) == (3, "OUT", "ERR")


def test_spawn_local_timeout_returns_124() -> None:
    rc, out, err = spawn_local(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin="",
        timeout=0.5,
        env=os.environ,
        cwd=None,
    )
    assert rc == 124 and out == "" and "timeout" in err


def test_spawn_local_spawn_failure_returns_127() -> None:
    rc, out, err = spawn_local(
        ["/nonexistent/definitely-not-a-real-binary-xyzzy"],
        stdin="",
        timeout=5,
        env=os.environ,
        cwd=None,
    )
    assert rc == 127 and out == "" and "spawn failed" in err


def test_host_dispatch_round_trips_stdin_and_stdout() -> None:
    # Prompt on stdin, echoed to stdout: proves the E2BIG-proof stdin channel
    # survives the refactor and the host path still produces a DispatchResult.
    prog = "import sys; sys.stdout.write('echo:' + sys.stdin.read())"
    res = dispatch(
        DispatchRequest(model="test", prompt="PING", timeout_s=30),
        [sys.executable, "-c", prog],
    )
    assert res.ok and res.rc == 0 and res.text == "echo:PING"


# ── 2. The A.1 invariant lives in spawn_local (single site) ─────────────────


def test_a1_tty_severance_lives_in_spawn_local() -> None:
    # Assert the terminal-severance guarantee at the primitive itself, so every
    # backend that reuses spawn_local inherits incident-Layer-1's fix.
    rc, out, err = spawn_local(
        [sys.executable, "-c", _TTY_PROBE],
        stdin="",
        timeout=30,
        env=os.environ,
        cwd=None,
    )
    assert rc == 0, err
    leader, tty = out.split(":")
    assert leader == "True"  # child is its own session leader → terminal severed
    assert tty == "NOTTY"  # and /dev/tty is unreachable from it


# ── 3. The delegation contract (non-host → Workspace.spawn) ─────────────────


class _RecordingWorkspace:
    """A fake ``Workspace`` that records its lifecycle and returns a fixed spawn
    triple — lets us prove core.dispatch drives up→spawn→down and finalizes the
    delegated ``(rc, stdout, stderr)`` without any container/daemon."""

    def __init__(self, *, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self._rc, self._stdout, self._stderr = rc, stdout, stderr
        self.calls: list = []

    def up(self) -> None:
        self.calls.append("up")

    def down(self) -> None:
        self.calls.append("down")

    def spawn(self, argv, *, stdin, timeout, env, cwd):
        self.calls.append(("spawn", tuple(argv), stdin, cwd, dict(env)))
        return self._rc, self._stdout, self._stderr

    # exec/put/get are unused by the spawn path; present for Protocol shape.
    def exec(self, cmd, *, cwd, timeout):  # pragma: no cover - unused here
        raise AssertionError("exec must not be called on the spawn path")

    def put(self, content):  # pragma: no cover
        raise NotImplementedError

    def get(self, path):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture()
def recording_backend() -> Iterator[_RecordingWorkspace]:
    """Register a shared recording workspace under a probe backend name and tear
    it out of the registry afterwards (no cross-test leak)."""
    ws = _RecordingWorkspace(
        rc=0,
        stdout='{"session_id": "sess-xyz", "result": "hello", "usage": {}}',
    )
    register_workspace_backend("fake-probe", lambda **_kw: ws)
    try:
        yield ws
    finally:
        sandbox._WORKSPACE_BACKENDS.pop("fake-probe", None)


def test_non_host_routes_through_workspace_spawn_lifecycle(
    recording_backend: _RecordingWorkspace,
) -> None:
    res = dispatch(
        DispatchRequest(
            model="test",
            prompt="PROMPT",
            workspace="fake-probe",
            env={"MO_PROBE": "1"},
        ),
        ["claude", "--print"],
        parse_text=claude_result_text,
        parse_session=claude_session_id,
    )
    # Lifecycle: provisioned, used once, torn down — in that order.
    kinds = [c[0] if isinstance(c, tuple) else c for c in recording_backend.calls]
    assert kinds == ["up", "spawn", "down"]
    # The engine owns the contract: argv + stdin prompt handed to the backend,
    # and the merged process env (request.env folded in) reaches spawn.
    _, argv, stdin, _cwd, env = recording_backend.calls[1]
    assert argv == ("claude", "--print") and stdin == "PROMPT"
    assert env.get("MO_PROBE") == "1"
    # The delegated triple flows through the SAME finalize as host: telemetry
    # parse is applied once, here, not in the backend.
    assert res.ok and res.text == "hello" and res.session_id == "sess-xyz"


def test_non_host_nonzero_rc_is_finalized_like_host() -> None:
    ws = _RecordingWorkspace(rc=65, stdout="", stderr="boom")
    register_workspace_backend("fake-fail", lambda **_kw: ws)
    try:
        res = dispatch(
            DispatchRequest(model="test", prompt="", workspace="fake-fail"),
            ["x"],
        )
    finally:
        sandbox._WORKSPACE_BACKENDS.pop("fake-fail", None)
    assert not res.ok and res.rc == 65 and res.error == "boom"
    assert ws.calls[0] == "up" and ws.calls[-1] == "down"  # torn down even on failure


def test_non_host_spawn_error_tears_down_and_propagates() -> None:
    class _RaisingWorkspace(_RecordingWorkspace):
        def spawn(self, argv, *, stdin, timeout, env, cwd):
            self.calls.append("spawn")
            raise NotImplementedError("not built yet")

    ws = _RaisingWorkspace()
    register_workspace_backend("fake-raise", lambda **_kw: ws)
    try:
        with pytest.raises(NotImplementedError):
            dispatch(
                DispatchRequest(model="test", prompt="", workspace="fake-raise"),
                ["x"],
            )
    finally:
        sandbox._WORKSPACE_BACKENDS.pop("fake-raise", None)
    # A loud failure must still not leak the workspace: down() ran in finally.
    assert ws.calls == ["up", "spawn", "down"]


def test_local_backend_spawn_equals_spawn_local() -> None:
    # The "local" backend's transport IS spawn_local — parity with the in-process
    # path, exercised through the Workspace seam.
    argv = [sys.executable, "-c", "import sys; sys.stdout.write('OK')"]
    via_seam = LocalWorkspace().spawn(argv, stdin="", timeout=30, env=os.environ, cwd=None)
    assert via_seam == (0, "OK", "")


def test_local_workspace_satisfies_protocol() -> None:
    # runtime_checkable Protocol now includes spawn; the built-in backend conforms.
    assert isinstance(LocalWorkspace(), Workspace)


# ── 4. Unbuilt backends fail loudly (Increments 2/3) ────────────────────────


def test_docker_spawn_stub_raises_not_implemented(tmp_path: Path) -> None:
    from mini_ork.runtime.backends.docker import DockerWorkspace

    ws = DockerWorkspace(image="alpine:latest", drive_root=str(tmp_path))
    with pytest.raises(NotImplementedError, match="Increment 2"):
        ws.spawn([sys.executable], stdin="", timeout=5, env={}, cwd=None)


def test_microvm_spawn_stub_raises_not_implemented(tmp_path: Path) -> None:
    from mini_ork.runtime.backends.microvm import MicrovmWorkspace

    ws = MicrovmWorkspace(image="alpine:latest", drive_root=str(tmp_path))
    with pytest.raises(NotImplementedError, match="Increment 3"):
        ws.spawn([sys.executable], stdin="", timeout=5, env={}, cwd=None)


# ── 5. The selector: resolution + precedence ────────────────────────────────


def test_resolve_spawn_workspace_rejects_host() -> None:
    # host must never reach the container resolver — it stays an in-process Popen.
    for name in ("host", ""):
        with pytest.raises(ValueError):
            resolve_spawn_workspace(name)


def test_resolve_spawn_workspace_local_is_localworkspace() -> None:
    assert isinstance(resolve_spawn_workspace("local"), LocalWorkspace)


def test_select_workspace_precedence() -> None:
    # 1. explicit non-host request wins outright, ignoring env.
    assert _select_workspace("docker", {"MO_SANDBOX_SCOPE": "tool"}) == "docker"
    # 2. scope=agent routes the CLI into the configured backend.
    assert (
        _select_workspace(
            "host", {"MO_SANDBOX_SCOPE": "agent", "MO_SANDBOX_BACKEND": "microvm"}
        )
        == "microvm"
    )
    # 3a. scope=tool → host (only tool-exec may be sandboxed).
    assert (
        _select_workspace(
            "host", {"MO_SANDBOX_SCOPE": "tool", "MO_SANDBOX_BACKEND": "microvm"}
        )
        == "host"
    )
    # 3b. scope=agent but no backend configured → host (nothing to isolate into).
    assert _select_workspace("host", {"MO_SANDBOX_SCOPE": "agent"}) == "host"
    # 3c. nothing configured → host.
    assert _select_workspace("host", {}) == "host"
