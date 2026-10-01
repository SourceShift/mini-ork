"""Unit tests for ``mini_ork.runtime.backends.remote`` (remote-nodes-06, kickoff §1).

Layered above the conformance suite: where ``test_workspace_conformance.py``
parameterises the SAME cases across registered backends (so a regression in
one is automatically a regression in all), this file is the unit layer that
proves the ``RemoteWorkspace``-specific contracts the conformance suite cannot
exercise without a host subprocess spy:

  * Retry policy: ``MO_REMOTE_HTTP_RETRIES`` exhausts to ``RemoteUnavailableError``
    with NO host fallback (the kickoff's "Never fall back to the host" clause).
  * Engine sha idempotency: a second ``up()`` for the same sha uploads nothing.
  * Dirty engine tree refuses by default; ``MO_REMOTE_ALLOW_DIRTY_ENGINE=1``
    bypasses.
  * Default path is byte-identical: ``MO_SANDBOX_BACKEND`` unset ->
    ``mini_ork.runtime.backends.remote`` is never imported.
  * ``RemoteUnavailableError`` propagates cleanly (never silently swallowed).

The shadow-merge proof lives in :mod:`test_nodes_yaml_shadow` (below) — the
"tabs of providers.yaml drops lanes" feedback tests it directly.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mini_ork.runtime.backends.remote import (
    RemoteUnavailableError,
    RemoteWorkspace,
    _NodeRef,
)


def _node(url: str = "http://127.0.0.1:1") -> _NodeRef:
    return _NodeRef(name="t", url=url, token="t-token", max_sessions=1)


# ---------------------------------------------------------------------------
# Module-import contract (review bar bullet 4: default path byte-identical).
# ---------------------------------------------------------------------------


def test_remote_module_not_imported_when_backend_unset(monkeypatch):
    """With ``MO_SANDBOX_BACKEND`` unset, the remote backend must not load.

    The resolver's empty-backend branch returns ``None`` BEFORE touching any
    backend module. Verified in an isolated subprocess so the test's own
    imports do not pre-load the module (which would mask a regression in
    in-process checks).
    """
    monkeypatch.delenv("MO_SANDBOX_BACKEND", raising=False)
    # Probe via a fresh interpreter so the resolver runs WITHOUT having
    # already loaded the remote backend (this test file imports it at the
    # top, which would otherwise mask any regression by pre-loading it).
    script = (
        "import sys\n"
        "from mini_ork.runtime.agent_workspace import (\n"
        "    resolve_agent_workspace, resolve_spawn_workspace,\n"
        ")\n"
        "ws, cwd = resolve_agent_workspace('/tmp', env={'PATH': ''})\n"
        "assert ws is None, f'unset backend should produce None, got {ws!r}'\n"
        "assert cwd == '/tmp', cwd\n"
        "local_ws = resolve_spawn_workspace('local', env={'PATH': ''})\n"
        "assert local_ws.__class__.__name__ == 'LocalWorkspace', local_ws\n"
        "assert 'mini_ork.runtime.backends.remote' not in sys.modules, "
        "'remote backend was loaded by an unset-backend path'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"isolated resolver probe failed:\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


def test_remote_module_not_imported_by_resolve_local():
    """Even explicit ``local`` resolution must not load the ``remote`` backend.

    Same isolated-subprocess probe as above, this time with explicit
    ``backend="local"`` so the resolver's lazy-import branch should NOT touch
    the remote backend module.
    """
    script = (
        "import sys\n"
        "from mini_ork.runtime.agent_workspace import resolve_spawn_workspace\n"
        "ws = resolve_spawn_workspace('local', env={'PATH': ''})\n"
        "assert ws.__class__.__name__ == 'LocalWorkspace', ws\n"
        "assert 'mini_ork.runtime.backends.remote' not in sys.modules, "
        "'resolve_spawn_workspace(local) loaded the remote backend'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"isolated resolver probe failed:\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Retry policy: bounded retries -> RemoteUnavailableError, NEVER a host fallback.
# ---------------------------------------------------------------------------


def test_remote_unavailable_after_retries_exhausted(monkeypatch, tmp_path):
    """After ``retries`` transport errors, raise ``RemoteUnavailableError``.

    ``urllib_request.urlopen`` is monkey-patched to always raise
    ``URLError`` — the precise shape the real socket raises on a refused
    connection. ``_request`` retries it ``retries`` times then converts
    to ``RemoteUnavailableError``; the test exercises that exact
    conversion (NOT a stub at ``_request``, which would mask the retry
    loop).
    """
    from urllib import request as _urlreq
    from urllib.error import URLError

    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    monkeypatch.setattr(RemoteWorkspace, "_DEFAULT_BACKOFF_S", 0.0, raising=False)
    # Bypass the real ``git rev-parse`` so the test does not depend on cwd.
    monkeypatch.setattr(
        "mini_ork.runtime.backends.remote.RemoteWorkspace._current_engine_sha",
        lambda self, subprocess_mod: "deadbeef00000000",
    )
    attempts = {"n": 0}

    def _always_fail(*_a, **_kw):
        attempts["n"] += 1
        raise URLError("connection refused")

    monkeypatch.setattr(_urlreq, "urlopen", _always_fail)

    ws = RemoteWorkspace(
        node=_node(), run_id="r", image="alpine:latest", drive_root=str(tmp_path),
        engine_root=os.getcwd(), retries=3,
    )
    with pytest.raises(RemoteUnavailableError):
        ws.up()
    # Three attempts (initial + 2 retries) before the third failure re-raises.
    assert attempts["n"] == 3


def test_remote_unavailable_propagates_through_exec(monkeypatch):
    """``exec`` surfaces ``RemoteUnavailableError``; it does not swallow it."""
    ws = RemoteWorkspace(
        node=_node(), run_id="r", image="alpine:latest", drive_root="/tmp",
        engine_root=os.getcwd(), retries=2,
    )
    ws._sid = "fake-sid"  # bypass up()

    def _fail(method, path, **kwargs):
        raise RemoteUnavailableError(f"transient {method} {path}")

    ws._request = _fail  # type: ignore[method-assign]
    with pytest.raises(RemoteUnavailableError):
        ws.exec("echo hi", cwd="/tmp", timeout=5)


# ---------------------------------------------------------------------------
# Engine sha idempotency (kickoff acceptance bullet 2).
# ---------------------------------------------------------------------------


def test_engine_sha_mismatch_uploads_exactly_one_bundle(monkeypatch, tmp_path):
    """First ``up()`` uploads ONE bundle; second ``up()`` for same sha uploads nothing.

    Uses a fake ``_current_engine_sha`` so the test does not depend on the
    surrounding git tree. ``PUT /v1/engines/{sha}`` is recorded.
    """
    monkeypatch.setattr(RemoteWorkspace, "_DEFAULT_BACKOFF_S", 0.0, raising=False)
    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    uploads: list[str] = []
    sessions: list[str] = []

    def _stub_request(method, path, *, body=None, content_type=None, query=None):
        if method == "PUT" and path.startswith("/v1/engines/"):
            uploads.append(path)
            return b""
        if method == "GET" and path == "/v1/health":
            # Pretend the node has no shas staged yet — every up() tries.
            return b'{"engine_shas": []}'
        if method == "POST" and path == "/v1/sessions":
            sessions.append(path)
            return b'{"sid": "fake-sid"}'
        raise RemoteUnavailableError(f"unexpected {method} {path}")

    ws = RemoteWorkspace(
        node=_node(), run_id="r", image="alpine:latest", drive_root=str(tmp_path),
        engine_root=os.getcwd(), retries=1,
    )
    ws._request = _stub_request  # type: ignore[method-assign]
    # Pin a stable sha so the test is hermetic.
    monkeypatch.setattr(
        "mini_ork.runtime.backends.remote.RemoteWorkspace._current_engine_sha",
        lambda self, subprocess_mod: "deadbeef00000000",
    )
    ws.up()
    ws.up()  # idempotent: same sha, no second upload
    ws.up()
    assert uploads == ["/v1/engines/deadbeef00000000"], uploads
    # Session create runs ONCE because the second ``up()`` short-circuits at
    # ``if self._sid is None``.
    assert sessions == ["/v1/sessions"], sessions


# ---------------------------------------------------------------------------
# Dirty engine tree refusal.
# ---------------------------------------------------------------------------


def test_dirty_engine_tree_refused_by_default(monkeypatch, tmp_path):
    """Without ``MO_REMOTE_ALLOW_DIRTY_ENGINE=1``, dirty trees raise loud."""
    # Make a tiny git repo and leave it dirty.
    repo = tmp_path / "engine"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f").write_text("a")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "i", "--quiet"], check=True)
    (repo / "f").write_text("dirty")
    monkeypatch.delenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", raising=False)
    ws = RemoteWorkspace(
        node=_node(), run_id="r", image="alpine:latest", drive_root=str(tmp_path),
        engine_root=str(repo), retries=1,
    )
    with pytest.raises(RemoteUnavailableError, match="dirty"):
        ws.up()


def test_dirty_engine_tree_allowed_with_env(monkeypatch, tmp_path):
    """With ``MO_REMOTE_ALLOW_DIRTY_ENGINE=1``, dirty trees proceed."""
    repo = tmp_path / "engine"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f").write_text("a")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "i", "--quiet"], check=True)
    (repo / "f").write_text("dirty")
    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    monkeypatch.setattr(RemoteWorkspace, "_DEFAULT_BACKOFF_S", 0.0, raising=False)

    def _stub(method, path, *, body=None, content_type=None, query=None):
        if method == "GET" and path == "/v1/health":
            return b'{"engine_shas": []}'
        if method == "POST" and path == "/v1/sessions":
            return b'{"sid": "fake-sid"}'
        if method == "PUT" and path.startswith("/v1/engines/"):
            return b""
        raise RemoteUnavailableError(f"unexpected {method} {path}")

    ws = RemoteWorkspace(
        node=_node(), run_id="r", image="alpine:latest", drive_root=str(tmp_path),
        engine_root=str(repo), retries=1,
    )
    ws._request = _stub  # type: ignore[method-assign]
    # Should NOT raise.
    ws.up()


# ---------------------------------------------------------------------------
# No host fallback (kickoff acceptance bullet 3).
# ---------------------------------------------------------------------------


def test_node_agent_down_raises_remote_unavailable_no_local_subprocess(monkeypatch, tmp_path):
    """With the node-agent unreachable, NO local subprocess runs.

    We use a sentinel ``subprocess.run`` spy at the module level. If the
    remote backend ever silently fell back to ``subprocess.run``, the spy
    would record the call. It does not.
    """
    monkeypatch.setattr(RemoteWorkspace, "_DEFAULT_BACKOFF_S", 0.0, raising=False)
    calls: list[tuple] = []
    real_run = subprocess.run

    def _spy_run(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    def _stub(method, path, *, body=None, content_type=None, query=None):
        raise RemoteUnavailableError(f"transient {method} {path}")

    ws = RemoteWorkspace(
        node=_node(), run_id="r", image="alpine:latest", drive_root=str(tmp_path),
        engine_root=os.getcwd(), retries=1,
    )
    ws._request = _stub  # type: ignore[method-assign]
    with pytest.raises(RemoteUnavailableError):
        ws.up()
    # The only subprocess calls permitted are the engine-sha / dirty checks
    # in ``_current_engine_sha``; we ran them against the real repo (cwd is
    # a git checkout). The contract we ASSERT here is that NO subprocess was
    # started that resembles a host-side fallback (no ``echo``, no
    # ``sh -c``). Filter for that.
    fallback_calls = [
        c for c in calls
        if any("sh" in str(p) or "echo" in str(p) or "spawn" in str(p) for p in c[0][:1])
    ]
    assert fallback_calls == [], fallback_calls


# ---------------------------------------------------------------------------
# Resolver routing (kickoff §3).
# ---------------------------------------------------------------------------


def test_resolve_spawn_workspace_routes_remote_to_remote_backend(monkeypatch):
    """``resolve_spawn_workspace("remote", ...)`` returns a ``RemoteWorkspace``.

    The resolver branches on backend; for ``"remote"`` it must lazy-import
    the backend and return a ``RemoteWorkspace`` instance, NOT a
    ``LocalWorkspace`` or a ``ValueError``.
    """
    from mini_ork.runtime.agent_workspace import resolve_spawn_workspace

    ws = resolve_spawn_workspace(
        "remote",
        env={
            "MO_NODE_URL": "http://127.0.0.1:7091",
            "MO_NODE_TOKEN": "t",
            "MO_SANDBOX_IMAGE": "alpine:latest",
            "MINI_ORK_RUN_ID": "r",
        },
    )
    assert isinstance(ws, RemoteWorkspace), ws
    assert ws._node.url == "http://127.0.0.1:7091"


def test_resolve_agent_workspace_routes_remote(monkeypatch):
    """``resolve_agent_workspace`` returns ``(RemoteWorkspace, MOUNT_PATH)`` for remote."""
    from mini_ork.runtime.agent_workspace import resolve_agent_workspace

    ws, cwd = resolve_agent_workspace(
        "/host/cwd",
        env={
            "MO_SANDBOX_BACKEND": "remote",
            "MO_NODE_URL": "http://127.0.0.1:7091",
            "MO_NODE_TOKEN": "t",
            "MO_SANDBOX_IMAGE": "alpine:latest",
            "MINI_ORK_RUN_ID": "r",
        },
    )
    assert isinstance(ws, RemoteWorkspace), ws
    assert cwd == "/workspace", cwd

def test_real_socket_transport_round_trip(tmp_path, monkeypatch):
    """RemoteWorkspace over a REAL uvicorn socket — no ``_request`` stub.

    The conformance suite swaps the transport for a TestClient; this is the one
    test that drives the production urllib path (auth header, retries, the
    JSON-lines stream parse) end to end against a live node-agent.
    """
    import socket
    import threading
    import time

    import uvicorn

    from mini_ork.remote.node_agent.app import create_app
    from mini_ork.runtime.backends.remote import RemoteWorkspace, _NodeRef

    token = "real-socket-token"
    monkeypatch.setenv("MO_NODE_TOKEN", token)
    monkeypatch.setenv("MO_REMOTE_ALLOW_DIRTY_ENGINE", "1")
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(state_dir=tmp_path / "agent", token_env="MO_NODE_TOKEN", runtime="host"),
        host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "node-agent did not start"
    ws = RemoteWorkspace(
        node=_NodeRef(name="real", url=f"http://127.0.0.1:{port}", token=token, max_sessions=1),
        run_id="real-socket-run", image="alpine:latest", drive_root=str(tmp_path),
        engine_root=os.environ.get("MINI_ORK_ROOT") or os.getcwd(),
        token_env="MO_NODE_TOKEN", retries=1,
    )
    try:
        ws.up()
        rc, out, err = ws.spawn(["/bin/sh", "-c", "echo out-line; echo err-line 1>&2; exit 3"],
                                stdin="", timeout=30, env={"PATH": os.environ.get("PATH", "")},
                                cwd=str(tmp_path))
        assert (rc, out.strip(), err.strip()) == (3, "out-line", "err-line")
        rc, merged = ws.exec("echo via-exec", cwd=str(tmp_path), timeout=30)
        assert rc == 0 and "via-exec" in merged
    finally:
        ws.down()
        server.should_exit = True
        thread.join(timeout=10)
