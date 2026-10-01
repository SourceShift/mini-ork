"""Run-scoped :class:`Workspace` registry (remote-nodes epic 02).

Each mini-ork run shares **one** workspace session across every node, retry, and
fallback lane; the workspace is torn down exactly once at run end (or on SIGTERM
/ ``kill_run``). Today's :func:`mini_ork.dispatch.core._spawn_in_workspace`
calls ``ws.up() → ws.spawn() → ws.down()`` per dispatch, so a single node that
walks a 2-lane fallback chain burns **2 containers** today; a remote boot costs
seconds plus a network round trip, so the per-spawn lifecycle is the dominant
overhead remotely. The kickoff (D2 in ``docs/architecture/remote-nodes.md``)
mandates a process-wide registry keyed by ``(run_id, backend)``.

Contract (intentionally minimal surface — only the methods the spawn hot path needs):

  * :func:`get_run_session` returns the same live :class:`Workspace` for every
    call with the same ``(run_id, backend)``; constructs + ``up()`` on the first
    call, returns the cached instance afterwards. Thread-safe because the parallel
    pool dispatches concurrently.
  * :func:`close_run_session` calls ``down()`` exactly once for the run. Idempotent
    and never raises — errors are logged at WARN. The 6 h TTL reaper
    (:mod:`mini_ork.runtime.sandbox_reaper`) is the safety net for crashes that
    bypass teardown.
  * The session marker file ``<run_dir>/.workspace-session.json`` records the
    ``{backend, session_id, created_at}`` so an external ``kill_run`` and
    ``sandbox-gc`` can find and reap it without traversing the live in-memory
    registry. The marker is written AFTER ``up()`` succeeds, BEFORE the cached
    workspace is returned to the caller.

Lazy-import contract: this module imports :class:`Workspace` (stdlib-only) at top
level, but **never** imports a concrete backend (``DockerWorkspace`` /
``MicrovmWorkspace`` / ``LocalWorkspace``). Backends are imported inside
:func:`get_run_session` so a host-path dispatch (``_select_workspace`` returns
``"host"``) never touches a backend's ``register()`` call. Verified by the
acceptance test: a fresh interpreter asserting the module is absent from
``sys.modules`` after a host dispatch.

Import-time is side-effect-free (no env read, no I/O, no backend registration) —
exactly the contract :mod:`mini_ork.runtime.sandbox` keeps for itself.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from mini_ork.runtime.sandbox import Workspace

__all__ = [
    "get_run_session",
    "close_run_session",
    "session_marker_path",
    "_reset_registry",
]


# --- registry state (process-wide, lazy-allocated) ----------------------------

_SESSION_REGISTRY: dict[tuple[str, str], "WorkspaceSession"] = {}
_SESSION_LOCK = threading.Lock()


def session_marker_path(run_dir: str | os.PathLike[str]) -> Path:
    """Path to the ``.workspace-session.json`` marker for a run.

    Centralized so ``control.kill_run`` and the executor's teardown agree on the
    filename. Returned path is OS-agnostic (forward-slash via ``Path``).
    """
    return Path(run_dir) / ".workspace-session.json"


class WorkspaceSession:
    """A live ``Workspace`` plus the metadata :func:`close_run_session` needs.

    Holds a single :class:`Workspace` instance for the lifetime of the run; the
    backend's ``up()`` is called once (in :func:`get_run_session`) and ``down()``
    is called once (in :func:`close_run_session`). The instance is reused
    across every :meth:`Workspace.spawn` call within the run.
    """

    __slots__ = ("workspace", "backend", "session_id", "run_dir", "closed")

    def __init__(
        self,
        workspace: Workspace,
        backend: str,
        session_id: str,
        run_dir: Path,
    ) -> None:
        self.workspace = workspace
        self.backend = backend
        self.session_id = session_id
        self.run_dir = run_dir
        self.closed = False


# --- backend resolution (lazy-import seam) -----------------------------------


def _resolve_backend_workspace(
    backend: str,
    *,
    env: Mapping[str, str],
    cwd: str | None,
) -> Workspace:
    """Resolve ``backend`` to a live :class:`Workspace`.

    Delegates to :func:`mini_ork.runtime.agent_workspace.resolve_spawn_workspace`
    which already owns the canonical image/drive/mount resolution and lazy
    backend imports — this module does NOT shadow it. A failure (``unknown
    backend`` / ``docker daemon down`` / ``no image configured``) propagates
    loudly: a misconfigured isolation backend is a setup error, not a retryable
    lane failure to mask.
    """
    # Lazy import: the dispatch hot path stays free of agent_workspace's import
    # graph (same seam :func:`_spawn_in_workspace` already uses). Backend modules
    # themselves are pulled in inside ``resolve_spawn_workspace``.
    from mini_ork.runtime.agent_workspace import resolve_spawn_workspace

    return resolve_spawn_workspace(backend, env=env, cwd=cwd)


def _extract_session_id(workspace: Workspace, backend: str) -> str:
    """Pull the backend's session id from its private state.

    Backends store their runtime id differently (``_cid`` for docker, the
    microVM name for microvm, ``None`` for local). The registry only needs a
    string the marker file + ``kill_run`` reap can match — the docker cid is
    the load-bearing case (it's what ``docker rm -f`` consumes). Other backends
    return their ``str(workspace)`` as a stable identifier; absent that, the
    run id alone is the marker.
    """
    if backend == "docker":
        cid = getattr(workspace, "_cid", None)
        if cid:
            return str(cid)
    name = getattr(workspace, "_name", None)
    if name:
        return str(name)
    # LocalWorkspace has no persistent id — empty string is fine, the marker
    # still records the backend so reapers can skip it.
    return ""


def _write_marker(run_dir: Path, payload: dict[str, Any]) -> None:
    """Atomically write the session marker (crash-safe via ``os.replace``)."""
    marker = session_marker_path(run_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, marker)


# --- public surface ----------------------------------------------------------


def get_run_session(
    run_id: str,
    backend: str,
    *,
    env: Mapping[str, str],
) -> Workspace:
    """Return the run-scoped :class:`Workspace` for ``(run_id, backend)``.

    On first call: resolve the backend's :class:`Workspace`, call ``up()``,
    write the session marker, and cache the instance. On subsequent calls
    (including from threads / asyncio tasks / parallel pool workers) the same
    instance is returned; ``up()`` is **not** called again — calling ``up()``
    twice on a docker backend fails with ``name in use`` and would burn a
    container per dispatch, which is exactly the regression this module exists
    to prevent.

    ``env`` is the dispatch-env snapshot the caller already has (typically
    ``context_env_snapshot()``). It is forwarded to the backend factory so
    image / drive / scope resolution stays consistent with the rest of the
    dispatch path. ``roots`` is currently advisory (kept for the future
    ``roots=`` kwarg the dispatch layer will pass); the backend factory does
    not consume it today.

    With an empty ``run_id`` (an ad-hoc dispatch or a test), no session is
    recorded and the call delegates to today's one-shot lifecycle: a fresh
    ``Workspace`` per call. This preserves the historical contract for
    ``ad-hoc dispatch or tests`` called out in kickoff requirement 2.
    """
    if not run_id:
        # No run id → behave like today: fresh workspace per call.
        ws = _resolve_backend_workspace(backend, env=env, cwd=None)
        ws.up()
        return ws

    key = (run_id, backend)
    with _SESSION_LOCK:
        existing = _SESSION_REGISTRY.get(key)
        if existing is not None:
            return existing.workspace

        # First call for this (run_id, backend): construct, up(), record marker.
        ws = _resolve_backend_workspace(backend, env=env, cwd=None)
        ws.up()
        run_dir = _resolve_run_dir(env, run_id)
        session = WorkspaceSession(
            workspace=ws,
            backend=backend,
            session_id=_extract_session_id(ws, backend),
            run_dir=run_dir,
        )
        try:
            _write_marker(
                run_dir,
                {
                    "backend": backend,
                    "session_id": session.session_id,
                    "created_at": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "run_id": run_id,
                },
            )
        except OSError:
            # Marker write is best-effort — the in-memory registry still owns
            # the workspace for the rest of the run. ``kill_run`` will lose
            # its targeted reap (falls back to TTL), but the run itself works.
            pass
        _SESSION_REGISTRY[key] = session
        return ws


def close_run_session(run_id: str) -> None:
    """Tear down every session registered for ``run_id``; never raises.

    Iterates over a snapshot of ``_SESSION_REGISTRY`` so a backend whose
    ``down()`` re-enters :func:`get_run_session` (it does not, today, but a
    future backend might) cannot deadlock the lock. For each session: if it
    has not already been closed, call ``down()`` and mark it closed; remove
    the session marker. Errors are caught and logged at WARN — the
    :func:`mini_ork.runtime.sandbox_reaper.reap_sandboxes` TTL reaper is the
    safety net for crashed runs that never reach here.
    """
    if not run_id:
        return
    with _SESSION_LOCK:
        sessions = [
            session for key, session in _SESSION_REGISTRY.items() if key[0] == run_id
        ]
        for key in list(_SESSION_REGISTRY):
            if key[0] == run_id:
                _SESSION_REGISTRY.pop(key, None)
    for session in sessions:
        if session.closed:
            continue
        session.closed = True
        try:
            session.workspace.down()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            print(
                f"[warn] close_run_session: down() failed for run={run_id} "
                f"backend={session.backend}: {exc}",
                file=sys.stderr,
            )
        try:
            session_marker_path(session.run_dir).unlink(missing_ok=True)
        except OSError:
            pass


# --- helpers -----------------------------------------------------------------


def _resolve_run_dir(env: Mapping[str, str], run_id: str) -> Path:
    """Resolve the marker file's parent directory.

    Prefers ``MINI_ORK_RUN_DIR`` from the env (the canonical env contract —
    the executor publishes it before any node runs); falls back to a
    ``.mini-ork/runs/<run_id>`` shape so callers that didn't publish the env
    var still get a writable path. ``run_id`` is interpolated into the
    fallback; the env contract guarantees a sane value.
    """
    env_dir = (env.get("MINI_ORK_RUN_DIR") or "").strip()
    if env_dir:
        return Path(env_dir)
    home = (env.get("MINI_ORK_HOME") or "").strip() or ".mini-ork"
    return Path(home) / "runs" / run_id


def _reset_registry() -> None:
    """Clear the registry (TEST ONLY).

    Lets a pytest fixture wipe state when many tests share the process-wide
    registry. Production code MUST NOT call this — closing a session is the
    only sanctioned teardown path.
    """
    with _SESSION_LOCK:
        for session in list(_SESSION_REGISTRY.values()):
            try:
                session.workspace.down()
            except Exception:  # noqa: BLE001
                pass
        _SESSION_REGISTRY.clear()