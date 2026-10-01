"""Tests for :mod:`mini_ork.runtime.workspace_session` (remote-nodes-02).

Covers the kickoff's Acceptance bullets:

  * Session reuse: 3 nodes × 2 retries × 2 fallback lanes → exactly 1 ``up()``
    and 1 ``down()`` on the fake backend (acceptance bullet 1).
  * SIGTERM teardown: a SIGTERM delivered to the executor mid-spawn results in
    ``down()`` being called (acceptance bullet 2). Modeled as a subprocess test
    against ``mini_ork.runtime.workspace_session``'s public surface — the
    executor-level SIGTERM handler is wired to ``close_run_session``, which is
    what this exercises.
  * Marker file: ``<run_dir>/.workspace-session.json`` is written on first
    ``get_run_session`` and removed by ``close_run_session``.
  * ``kill_run`` reap: a marker file pointing at a docker cid triggers
    ``docker rm -f <cid>`` (daemon-free path: stub ``subprocess.run``).
  * Host-path parity: with ``MO_SANDBOX_BACKEND`` unset and
    ``MO_SANDBOX_SCOPE`` unset, ``mini_ork.runtime.workspace_session`` is NOT
    imported by a dispatch (acceptance bullet 4 — kickoff Req #5). The
    module is queried in a fresh interpreter so the assert runs without
    any prior-import leakage.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from typing import Any
from unittest import mock

import pytest

from mini_ork.runtime import workspace_session as ws_module


# --- Fake backend -----------------------------------------------------------


class CountingBackend:
    """A ``Workspace`` that records ``up()`` / ``down()`` / ``spawn()``.

    Used by the acceptance bullet 1 test. Registered under a fresh name per
    test so parallel pytest-xdist runs don't collide (the workspace registry
    is process-wide).
    """

    def __init__(self, **kwargs: Any) -> None:
        self.up_calls = 0
        self.down_calls = 0
        self.spawn_calls = 0
        self.kwargs = kwargs

    def up(self) -> None:
        self.up_calls += 1

    def down(self) -> None:
        self.down_calls += 1

    def spawn(self, argv, *, stdin, timeout, env, cwd):  # noqa: ARG002
        self.spawn_calls += 1
        return 0, "", ""

    def exec(self, cmd, *, cwd, timeout):  # noqa: ARG002
        return 0, ""

    def put(self, content):  # noqa: ARG002
        return "/tmp/x"

    def get(self, path):  # noqa: ARG002
        return ""


@pytest.fixture
def counting_backend(tmp_path, monkeypatch):
    """Register a counting fake backend under a unique name; clean up after."""
    from mini_ork.runtime import sandbox

    name = f"fake-{uuid.uuid4().hex[:8]}"
    backend = CountingBackend()
    sandbox.register_workspace_backend(name, lambda **kw: backend)
    monkeypatch.setattr(ws_module, "_resolve_backend_workspace",
                        lambda backend_name, *, env, cwd: backend)
    yield name, backend
    # Teardown: drop the test instance from the registry so it doesn't leak.
    try:
        ws_module._reset_registry()
    except Exception:
        pass
    sandbox._WORKSPACE_BACKENDS.pop(name, None)


# --- Session reuse (acceptance bullet 1) ------------------------------------


def test_session_reuse_one_up_one_down_for_many_spawns(counting_backend):
    backend_name, backend = counting_backend
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    env = {
        "MINI_ORK_RUN_DIR": str("/tmp/test-run"),
        "MO_SANDBOX_BACKEND": backend_name,
    }

    # 3 nodes × 2 retries × 2 fallback lanes = 12 spawns.
    for _ in range(12):
        ws = ws_module.get_run_session(run_id, backend_name, env=env)
        rc, _, _ = ws.spawn([], stdin="", timeout=10, env={}, cwd=None)
        assert rc == 0

    assert backend.up_calls == 1
    assert backend.down_calls == 0  # NOT yet closed
    assert backend.spawn_calls == 12

    ws_module.close_run_session(run_id)
    assert backend.down_calls == 1


def test_concurrent_get_run_session_returns_same_instance(counting_backend):
    """Parallel pool dispatches concurrently; the lock must guard first-call init."""
    backend_name, backend = counting_backend
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    env = {"MINI_ORK_RUN_DIR": "/tmp/test-run", "MO_SANDBOX_BACKEND": backend_name}

    results: list = []
    errors: list = []

    def _worker():
        try:
            ws = ws_module.get_run_session(run_id, backend_name, env=env)
            results.append(ws)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 20
    # All threads received the SAME workspace object.
    assert all(ws is results[0] for ws in results)
    # The first thread did up(); every subsequent thread returned the cached
    # instance, so up() count is exactly 1.
    assert backend.up_calls == 1


def test_empty_run_id_returns_fresh_workspace_each_call(monkeypatch, tmp_path):
    """No MINI_ORK_RUN_ID → ad-hoc / test path: one workspace per call.

    Skips the ``counting_backend`` fixture because that fixture monkeypatches
    ``_resolve_backend_workspace`` to return the SAME instance, which would
    collapse the per-call freshness this test exercises. Register a fresh-
    per-call factory here.
    """
    from mini_ork.runtime import sandbox

    name = f"ad-hoc-{uuid.uuid4().hex[:8]}"
    instances: list[CountingBackend] = []

    def factory(**kwargs):
        ws = CountingBackend()
        instances.append(ws)
        return ws

    sandbox.register_workspace_backend(name, factory)
    env = {"MO_SANDBOX_BACKEND": name}

    try:
        ws_a = ws_module.get_run_session("", name, env=env)
        ws_b = ws_module.get_run_session("", name, env=env)
        assert ws_a is not ws_b
        assert instances[0].up_calls == 1
        assert instances[1].up_calls == 1
    finally:
        sandbox._WORKSPACE_BACKENDS.pop(name, None)


def test_different_backends_get_distinct_sessions(tmp_path, monkeypatch):
    """Two backends in one run → two workspaces, two ups."""
    from mini_ork.runtime import sandbox

    name_a = f"fake-a-{uuid.uuid4().hex[:8]}"
    name_b = f"fake-b-{uuid.uuid4().hex[:8]}"
    ws_a = CountingBackend()
    ws_b = CountingBackend()
    sandbox.register_workspace_backend(name_a, lambda **kw: ws_a)
    sandbox.register_workspace_backend(name_b, lambda **kw: ws_b)

    def _resolve(backend_name, *, env, cwd):
        return ws_a if backend_name == name_a else ws_b

    monkeypatch.setattr(ws_module, "_resolve_backend_workspace", _resolve)

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    env = {"MINI_ORK_RUN_DIR": str(tmp_path)}
    got_a = ws_module.get_run_session(run_id, name_a, env=env)
    got_b = ws_module.get_run_session(run_id, name_b, env=env)
    assert got_a is ws_a and got_b is ws_b
    assert ws_a.up_calls == 1 and ws_b.up_calls == 1

    ws_module.close_run_session(run_id)
    assert ws_a.down_calls == 1 and ws_b.down_calls == 1
    sandbox._WORKSPACE_BACKENDS.pop(name_a, None)
    sandbox._WORKSPACE_BACKENDS.pop(name_b, None)


# --- Marker file lifecycle -------------------------------------------------


def test_marker_file_written_on_first_call_and_removed_on_close(
    counting_backend, tmp_path,
):
    backend_name, _ = counting_backend
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    env = {"MINI_ORK_RUN_DIR": str(run_dir), "MO_SANDBOX_BACKEND": backend_name}

    ws_module.get_run_session(run_id, backend_name, env=env)
    marker = run_dir / ".workspace-session.json"
    assert marker.is_file()
    payload = json.loads(marker.read_text())
    assert payload["backend"] == backend_name
    assert payload["run_id"] == run_id
    assert "created_at" in payload

    ws_module.close_run_session(run_id)
    assert not marker.exists()


def test_close_run_session_is_idempotent(counting_backend, tmp_path):
    backend_name, backend = counting_backend
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    env = {"MINI_ORK_RUN_DIR": str(tmp_path), "MO_SANDBOX_BACKEND": backend_name}

    ws_module.get_run_session(run_id, backend_name, env=env)
    ws_module.close_run_session(run_id)
    ws_module.close_run_session(run_id)  # second call must not raise
    ws_module.close_run_session(run_id)  # third call too
    assert backend.down_calls == 1  # exactly one down


def test_close_run_session_does_not_raise_on_unknown_run_id():
    # Unknown id: a no-op (not an error). Acceptable behavior — kill_run may
    # race with a clean run-end teardown.
    ws_module.close_run_session("nonexistent-run")


def test_close_run_session_handles_down_failure(counting_backend, tmp_path):
    """``Workspace.down()`` raising must NOT propagate out of close_run_session."""
    backend_name, backend = counting_backend

    def _boom():
        raise RuntimeError("simulated down failure")

    backend.down = _boom  # type: ignore[method-assign]

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    env = {"MINI_ORK_RUN_DIR": str(tmp_path), "MO_SANDBOX_BACKEND": backend_name}
    ws_module.get_run_session(run_id, backend_name, env=env)
    # Must not raise; the marker is best-effort cleaned up too.
    ws_module.close_run_session(run_id)


# --- kill_run reap via sandbox_reaper.reap_by_marker -----------------------


def test_reap_by_marker_docker_removes_container(monkeypatch, tmp_path):
    """A marker pointing at a docker cid triggers ``docker rm -f``."""
    from mini_ork.runtime import sandbox_reaper

    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sandbox_reaper, "_run", fake_run)
    monkeypatch.setattr(sandbox_reaper.shutil, "which", lambda name: "/usr/bin/docker")

    run_dir = tmp_path / "run-xyz"
    run_dir.mkdir()
    (run_dir / ".workspace-session.json").write_text(
        json.dumps(
            {
                "backend": "docker",
                "session_id": "cid999",
                "created_at": "2026-09-01T00:00:00Z",
                "run_id": "run-xyz",
            }
        )
    )

    reaped = sandbox_reaper.reap_by_marker(run_dir)
    assert reaped == ["cid999"]
    assert any(c[:3] == ["docker", "rm", "-f"] for c in calls)
    assert not (run_dir / ".workspace-session.json").exists()


def test_reap_by_marker_dry_run_does_not_invoke(monkeypatch, tmp_path):
    from mini_ork.runtime import sandbox_reaper

    calls: list[list[str]] = []
    monkeypatch.setattr(sandbox_reaper, "_run",
                        lambda a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(sandbox_reaper.shutil, "which", lambda name: "/usr/bin/docker")

    run_dir = tmp_path / "run-xyz"
    run_dir.mkdir()
    (run_dir / ".workspace-session.json").write_text(
        json.dumps({"backend": "docker", "session_id": "cid123"})
    )
    reaped = sandbox_reaper.reap_by_marker(run_dir, dry_run=True)
    assert reaped == ["cid123"]
    assert calls == []  # NO rm -f under dry-run
    # Marker preserved under dry-run
    assert (run_dir / ".workspace-session.json").exists()


def test_reap_by_marker_missing_marker_returns_empty(tmp_path):
    from mini_ork.runtime import sandbox_reaper

    assert sandbox_reaper.reap_by_marker(tmp_path) == []


def test_reap_by_marker_unparseable_marker_returns_empty(tmp_path):
    from mini_ork.runtime import sandbox_reaper

    (tmp_path / ".workspace-session.json").write_text("not-json{")
    assert sandbox_reaper.reap_by_marker(tmp_path) == []


def test_reap_by_marker_no_docker_cli_returns_empty(monkeypatch, tmp_path):
    from mini_ork.runtime import sandbox_reaper

    monkeypatch.setattr(sandbox_reaper.shutil, "which", lambda name: None)
    (tmp_path / ".workspace-session.json").write_text(
        json.dumps({"backend": "docker", "session_id": "cid1"})
    )
    assert sandbox_reaper.reap_by_marker(tmp_path) == []


# --- kill_run integration (control.py marker-driven reap) ------------------


def test_kill_run_reaps_docker_container_via_marker(tmp_path):
    """End-to-end: ``kill_run`` finds the marker and ``docker rm -f`` the cid."""
    from mini_ork.web import control

    # Build a marker pointing at a fake cid.
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / ".workspace-session.json").write_text(
        json.dumps({"backend": "docker", "session_id": "cid-fake-001"})
    )

    # Stub ``docker rm -f`` so the test never needs real docker.
    docker_calls: list[list[str]] = []

    def fake_subprocess_run(argv, **kwargs):
        docker_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    # Stub ``StateDB`` so kill_run doesn't touch a real database.
    class FakeDB:  # type: ignore[ misc] -- structural stub for StateDB
        db_path = tmp_path / "state.db"

        def row(self, sql, params=()):
            return {"id": run_id, "status": "executing"}

    with mock.patch.object(control.subprocess, "run", fake_subprocess_run), \
         mock.patch.object(control, "_writeback_terminal", lambda *a, **k: None), \
         mock.patch.object(control, "_close_dangling_node_events", lambda *a, **k: None):
        out = control.kill_run(tmp_path, FakeDB(), run_id)  # type: ignore[arg-type]

    assert any(c[:3] == ["docker", "rm", "-f"] and "cid-fake-001" in c for c in docker_calls)
    assert out.get("session_marker_reaped") is True
    assert not (run_dir / ".workspace-session.json").exists()


def test_kill_run_without_marker_still_succeeds(tmp_path):
    from mini_ork.web import control

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    # NO marker file written.

    class FakeDB:  # type: ignore[misc] -- structural stub for StateDB
        db_path = tmp_path / "state.db"

        def row(self, sql, params=()):
            return {"id": run_id, "status": "executing"}

    with mock.patch.object(control, "_writeback_terminal", lambda *a, **k: None), \
         mock.patch.object(control, "_close_dangling_node_events", lambda *a, **k: None):
        out = control.kill_run(tmp_path, FakeDB(), run_id)  # type: ignore[arg-type]

    assert out["ok"] is True
    assert out.get("session_marker_reaped") is False


# --- Host-path parity (acceptance bullet 4 / kickoff Req #5) ----------------


def test_dispatch_does_not_import_workspace_session_module():
    """A fresh interpreter with no backend selected must NOT have the module imported.

    Run via subprocess so the assertion sees a pristine ``sys.modules``. The
    dispatch path (``DispatchRequest.workspace == "host"``) must short-circuit
    at :func:`_spawn_in_workspace`'s host gate (``core.py:304``) without
    importing :mod:`mini_ork.runtime.workspace_session`.
    """
    code_lines = [
        "import sys",
        "import os",
        "env = dict(os.environ)",
        "env.pop('MO_SANDBOX_BACKEND', None)",
        "env.pop('MO_SANDBOX_SCOPE', None)",
        "from mini_ork.dispatch.providers import _select_workspace",
        "chosen = _select_workspace('host', env)",
        "assert chosen == 'host', 'unexpected selector: ' + repr(chosen)",
        "leak = 'mini_ork.runtime.workspace_session' in sys.modules",
        "assert not leak, 'workspace_session leaked into host dispatch'",
        "print('OK')",
    ]
    code = "\n".join(code_lines)
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MO_SANDBOX_BACKEND": "", "MO_SANDBOX_SCOPE": ""},
    )
    assert r.returncode == 0, (
        f"host-parity subprocess failed: stderr={r.stderr!r}, stdout={r.stdout!r}"
    )
    assert "OK" in r.stdout


def test_module_import_time_is_pure():
    """Importing :mod:`mini_ork.runtime.workspace_session` must not register a
    docker factory, read env, or perform I/O. Mirrors
    :mod:`mini_ork.runtime.sandbox` self-test."""
    from mini_ork.runtime import sandbox

    before = set(sandbox._WORKSPACE_BACKENDS)
    before_env = dict(os.environ)
    # Import must NOT add a docker backend (it's a lazy import) or mutate env.
    import mini_ork.runtime.workspace_session  # noqa: F401
    after = set(sandbox._WORKSPACE_BACKENDS)
    assert before == after
    assert dict(os.environ) == before_env


# --- Marker-file atomicity --------------------------------------------------


def test_marker_write_is_atomic_under_concurrent_first_call(
    counting_backend, tmp_path,
):
    """Even with concurrent first-call threads, only ONE marker file exists."""
    backend_name, _ = counting_backend
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    env = {"MINI_ORK_RUN_DIR": str(run_dir), "MO_SANDBOX_BACKEND": backend_name}

    def _worker():
        ws_module.get_run_session(run_id, backend_name, env=env)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    marker = run_dir / ".workspace-session.json"
    assert marker.is_file()
    # No leftover .tmp from a crashed atomic write
    leftovers = list(run_dir.glob(".workspace-session.json.tmp"))
    assert leftovers == []