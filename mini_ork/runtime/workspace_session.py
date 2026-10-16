"""Run-scoped :class:`Workspace` registry (remote-nodes epic 02).

Each mini-ork run shares **one** workspace session across every node, retry, and
fallback lane; the workspace is torn down exactly once at run end (or on SIGTERM
/ ``kill_run``). Today's :func:`mini_ork.dispatch.core._spawn_in_workspace`
calls ``ws.up() → ws.spawn() → ws.down()`` per dispatch, so a single node that
walks a 2-lane fallback chain burns **2 containers** today; a remote boot costs
seconds plus a network round trip, so the per-spawn lifecycle is the dominant
overhead remotely. The kickoff (D2 in ``docs/architecture/remote-nodes.md``)
mandates a process-wide registry keyed by ``(run_id, backend)``.

**Per-node sessions.** The key gains a third element, the node id, when
``MO_NODE_ISOLATION`` is on: ``(run_id, backend, node_id)`` gives each workflow
node its own sandbox, so no two nodes of a run share a machine (see
``docs/architecture/per-node-sandboxes.md``). This is the only place that can
deliver it — the registry caches one workspace per key and calls ``up()`` once,
so a per-node provider that resolved correctly but was cached under the run's
single key would hand every node the first node's sandbox. Isolation off (the
default) keys on ``""`` and behaves exactly as before.

Contract (intentionally minimal surface — only the methods the spawn hot path needs):

  * :func:`get_run_session` returns the same live :class:`Workspace` for every
    call with the same ``(run_id, backend, node_id)``; constructs + ``up()`` on
    the first call, returns the cached instance afterwards. Thread-safe because
    the parallel pool dispatches concurrently.
  * :func:`close_run_session` calls ``down()`` exactly once for the run. Idempotent
    and never raises — errors are logged at WARN. The 6 h TTL reaper
    (:mod:`mini_ork.runtime.sandbox_reaper`) is the safety net for crashes that
    bypass teardown.
  * The session marker file ``<run_dir>/.workspace-session.json`` records the
    ``{backend, session_id, created_at}`` so an external ``kill_run`` and
    ``sandbox-gc`` can find and reap it without traversing the live in-memory
    registry. The marker is written AFTER ``up()`` succeeds, BEFORE the cached
    workspace is returned to the caller. A per-node session writes
    ``<run_dir>/.workspace-session.<node_id>.json`` beside it, one per node:
    a single shared file would keep only the node that registered last, and the
    reapers that read it would leak every other node's sandbox to its TTL.

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
    "read_session_marker",
    "session_marker_path",
    "session_marker_paths",
    "_reset_registry",
]


# --- registry state (process-wide, lazy-allocated) ----------------------------

#: The registry key: ``(run_id, backend)`` for a run's shared session, plus the
#: node id for a per-node session (always ``""`` when isolation is off).
_SessionKey = tuple[str, str, str]

_SESSION_REGISTRY: dict[_SessionKey, "WorkspaceSession"] = {}
_SESSION_LOCK = threading.Lock()

# A backend whose session must OUTLIVE this process's handle: the run lifecycle
# (rubric / verify) still runs checks on the node and releases the session at
# the end of the run, so ``close_run_session(keep_remote=True)`` drops only this
# handle and keeps the marker for the later processes and ``kill_run``. Listed
# once here so no teardown branch names a backend (cloud-providers A2).
_SESSION_OUTLIVES_HANDLE = frozenset({"remote"})

_SESSION_MARKER = ".workspace-session.json"
_MARKER_PREFIX = ".workspace-session."
_MARKER_SUFFIX = ".json"


def session_marker_path(run_dir: str | os.PathLike[str], node_id: str = "") -> Path:
    """Path to the workspace-session marker for a run — or for one node of it.

    Centralized so ``control.kill_run`` and the executor's teardown agree on the
    filename. Returned path is OS-agnostic (forward-slash via ``Path``).

    ``node_id`` empty gives the run's marker, ``.workspace-session.json``, byte
    for byte the path this function returned before per-node sessions existed. A
    named node gets ``.workspace-session.<node_id>.json`` beside it. The node id
    is sanitized to one path segment — it reaches us from the workflow, and a
    ``../`` in it would put a marker outside the run dir.
    """
    base = Path(run_dir)
    if not node_id:
        return base / _SESSION_MARKER
    from mini_ork.remote.providers.run_nodes import safe_node_id

    return base / f"{_MARKER_PREFIX}{safe_node_id(node_id)}{_MARKER_SUFFIX}"


def _is_session_marker(name: str) -> bool:
    """Whether a directory entry is a session marker — never the ``.tmp`` half
    of an in-flight atomic write (that name ends ``.tmp``, not ``.json``)."""
    if name == _SESSION_MARKER:
        return True
    return name.startswith(_MARKER_PREFIX) and name.endswith(_MARKER_SUFFIX)


def session_marker_paths(run_dir: str | os.PathLike[str]) -> list[Path]:
    """Every session marker for a run: the run-level one first, then per-node.

    The run-level marker sorts first so a caller that wants only "the run's
    node" — the run-detail payload, the nodes page — can take ``[0]`` and read
    what it read before per-node sessions existed. Reapers walk the whole list.
    """
    base = Path(run_dir)
    try:
        found = [p for p in base.iterdir() if p.is_file() and _is_session_marker(p.name)]
    except OSError:  # an unreadable run dir holds no markers we can act on
        return []
    return sorted(found, key=lambda p: (p.name != _SESSION_MARKER, p.name))


class WorkspaceSession:
    """A live ``Workspace`` plus the metadata :func:`close_run_session` needs.

    Holds a single :class:`Workspace` instance for the lifetime of the run; the
    backend's ``up()`` is called once (in :func:`get_run_session`) and ``down()``
    is called once (in :func:`close_run_session`). The instance is reused
    across every :meth:`Workspace.spawn` call within the run.

    ``node_id`` is the node this session is scoped to, or ``""`` for the run's
    shared session. It decides which marker file the session owns, so teardown
    deletes its own marker and not a sibling node's.
    """

    __slots__ = ("workspace", "backend", "session_id", "run_dir", "closed", "node_id")

    def __init__(
        self,
        workspace: Workspace,
        backend: str,
        session_id: str,
        run_dir: Path,
        node_id: str = "",
    ) -> None:
        self.workspace = workspace
        self.backend = backend
        self.session_id = session_id
        self.run_dir = run_dir
        self.node_id = node_id
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


def _node_locator(workspace: Workspace) -> dict[str, Any]:
    """Where a remote session lives, for an out-of-process ``kill_run``: the
    node's name + URL and the NAME of the env var holding its token — never the
    token itself (the marker is a file on disk).

    A node OBTAINED from a provider also records the provider name and its
    handle, so the marker-driven reaper can route teardown through the provider
    seam (attach + release) instead of only the node URL. Names and ids only —
    never a token, never a provider auth header value (cloud-sandbox axis)."""
    node = getattr(workspace, "_node", None)
    provider = str(getattr(workspace, "_endpoint_provider", "") or "")
    handle = str(getattr(workspace, "_provider_handle", "") or "")
    if node is None or not getattr(node, "url", ""):
        # Provider-backed but not yet adopted (e.g. a teardown constructed a
        # workspace): name the provider so teardown can still route correctly.
        return {"provider": provider} if provider else {}
    locator: dict[str, Any] = {
        "node": {"name": getattr(node, "name", ""), "url": node.url},
        "token_env": getattr(workspace, "_token_env", "MO_NODE_TOKEN"),
    }
    if provider:
        locator["provider"] = provider
    if handle:
        locator["handle"] = handle
    return locator


def _extract_session_id(workspace: Workspace) -> str:
    """Pull the backend's session id from its own state — no backend-name branch.

    A backend that exposes a callable ``session_id()`` is asked for it (the
    contract cloud-providers A2 adds to ``RemoteWorkspace``); every other
    backend stores its runtime id in private state (``_cid`` for docker, the
    microVM name for microvm, ``None`` for local), so we take the first
    non-empty of ``_cid`` / ``_sid`` / ``_name``. The registry only needs a
    string the marker file + ``kill_run`` reap can match — the docker cid is
    the load-bearing case (it's what ``docker rm -f`` consumes). ``""`` is
    fine for a backend with no persistent id (local): the marker still records
    the backend so reapers can skip it.
    """
    getter = getattr(workspace, "session_id", None)
    if callable(getter):
        try:
            value = str(getter() or "").strip()
        except Exception:  # noqa: BLE001 — an id is best-effort marker metadata
            value = ""
        if value:
            return value
    for attr in ("_cid", "_sid", "_name"):
        value = getattr(workspace, attr, None)
        if value:
            return str(value)
    return ""


def read_session_marker(run_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """The run's session-marker payload, or ``{}`` when none is readable.

    Prefers the run-level marker, then the first per-node one (sorted), so a
    reader asking "what node is this run on" keeps its exact old answer for a
    shared-sandbox run and gets a real node — rather than nothing — for a
    per-node one. Unreadable or mistyped markers are skipped, not raised: this
    feeds display payloads and a best-effort teardown hint.
    """
    for marker in session_marker_paths(run_dir):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _write_marker(run_dir: Path, payload: dict[str, Any], *, node_id: str = "") -> None:
    """Atomically write the session marker (crash-safe via ``os.replace``)."""
    marker = session_marker_path(run_dir, node_id)
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
    node_id = _isolated_node(env)   # "" unless per-node sandboxing is on
    if not run_id:
        # No run id → behave like today: fresh workspace per call.
        ws = _resolve_backend_workspace(backend, env=env, cwd=None)
        ws.up()
        return ws

    key = (run_id, backend, node_id)
    with _SESSION_LOCK:
        existing = _SESSION_REGISTRY.get(key)
        if existing is not None:
            return existing.workspace

        # First call for this key: construct, up(), record marker.
        ws = _resolve_backend_workspace(backend, env=env, cwd=None)
        try:
            ws.up()
        except BaseException:
            # A half-provisioned session (e.g. created, then the initial sync
            # failed) is never registered, so nothing else would tear it down —
            # and on a remote node it holds a max_sessions slot until the TTL.
            try:
                ws.down()
            except Exception:  # noqa: BLE001 — the up() error is the one to report
                pass
            raise
        _register(run_id, backend, ws, env, node_id=node_id)
        return ws


def _isolated_node(env: Mapping[str, str]) -> str:
    """The node this session must be scoped to, or ``""`` for the run's shared
    one. Delegates to the provider-side policy helper so the registry and
    ``RemoteWorkspace.ensure_node`` answer the same question with one rule."""
    from mini_ork.remote.providers.run_nodes import isolated_node

    return isolated_node(env)


def register_run_session(run_id: str, backend: str, ws: Workspace, *,
                         env: Mapping[str, str], node_id: str = "") -> None:
    """Adopt an ALREADY-UP workspace as ``run_id``'s session, so dispatches for
    that run reuse it and :func:`close_run_session` tears it down. For callers
    that must watch provisioning themselves (``mini-ork nodes doctor``).

    ``node_id`` registers a per-node session under the key a per-node dispatch
    would look up; the default registers the run's shared one, as before."""
    with _SESSION_LOCK:
        if (run_id, backend, node_id) in _SESSION_REGISTRY:
            raise RuntimeError(
                f"run {run_id!r} already has a {backend} session"
                + (f" for node {node_id!r}" if node_id else "")
            )
        _register(run_id, backend, ws, env, node_id=node_id)


def _register(run_id: str, backend: str, ws: Workspace, env: Mapping[str, str], *,
              node_id: str = "") -> None:
    """Record ``ws`` + its marker (caller holds ``_SESSION_LOCK``)."""
    key = (run_id, backend, node_id)
    run_dir = _resolve_run_dir(env, run_id)
    session = WorkspaceSession(
        workspace=ws,
        backend=backend,
        session_id=_extract_session_id(ws),
        run_dir=run_dir,
        node_id=node_id,
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
                **({"node_id": node_id} if node_id else {}),
                **_node_locator(ws),
            },
            node_id=node_id,
        )
    except OSError:
        # Marker write is best-effort — the in-memory registry still owns
        # the workspace for the rest of the run. ``kill_run`` will lose
        # its targeted reap (falls back to TTL), but the run itself works.
        pass
    _SESSION_REGISTRY[key] = session


def release_remote_session(run_id: str, run_dir: str, *, env: Mapping[str, str]) -> None:
    """End a remote run's session on the node, whichever process created it.

    Every dispatch / check of a run is its own process with its own workspace;
    they share ONE node session (the node keys sessions by run id). This
    closes this process's handle, then deletes the node session by run id and
    drops the run dir's session marker + sync state. Best-effort.

    A run whose node came from a provider also DESTROYS that node here, via
    :func:`release_run_node` against the run dir's provider record — a static
    node (no record) is left for the operator, as before.

    A run that used per-node sandboxes destroys them all in the same breath, via
    :func:`release_all_nodes` over ``<run_dir>/.nodes/``: those are keyed on
    ``(run_id, node_id)``, so the run-level release above cannot see them, and
    the records on disk — not an env flag — decide what is freed."""
    close_run_session(run_id)
    from mini_ork.runtime.backends.remote import _factory

    ws = _factory(env={**env, "MINI_ORK_RUN_ID": run_id, "MINI_ORK_RUN_DIR": run_dir})
    ws._sid = run_id          # the node accepts the run id in DELETE /v1/sessions/{id}
    ws.down()
    try:
        from mini_ork.remote.providers.base import release_run_node

        release_run_node(run_dir, env)
    except Exception:  # noqa: BLE001 — teardown is best-effort; TTL reaper is the net
        pass
    try:
        from mini_ork.remote.providers.run_nodes import release_all_nodes

        release_all_nodes(run_dir, env)
    except Exception:  # noqa: BLE001 — same contract: best-effort, TTL is the net
        pass
    if run_dir:
        for name in (".remote-sync-state.json",):
            try:
                os.unlink(os.path.join(run_dir, name))
            except OSError:
                pass
        try:
            for marker in session_marker_paths(run_dir):
                marker.unlink(missing_ok=True)
        except OSError:
            pass


def close_run_session(run_id: str, *, lock_timeout: float | None = None,
                      keep_remote: bool = False) -> None:
    """Tear down every session registered for ``run_id``; never raises.

    Iterates over a snapshot of ``_SESSION_REGISTRY`` so a backend whose
    ``down()`` re-enters :func:`get_run_session` (it does not, today, but a
    future backend might) cannot deadlock the lock. For each session: if it
    has not already been closed, call ``down()`` and mark it closed; remove
    the session marker. Errors are caught and logged at WARN — the
    :func:`mini_ork.runtime.sandbox_reaper.reap_sandboxes` TTL reaper is the
    safety net for crashed runs that never reach here.

    ``lock_timeout`` is for the signal path. A handler runs on the main thread
    between bytecodes, possibly while that same thread holds the
    (non-reentrant) registry lock inside :func:`get_run_session`; a blocking
    acquire there would hang the process on SIGTERM. With a timeout, a busy
    lock leaves the teardown to the run-level ``finally`` / TTL reaper instead.
    """
    if not run_id:
        return
    if lock_timeout is None:
        _SESSION_LOCK.acquire()
    elif not _SESSION_LOCK.acquire(timeout=lock_timeout):
        print(
            f"[warn] close_run_session: registry busy for run={run_id}; "
            "leaving teardown to the TTL reaper",
            file=sys.stderr,
        )
        return
    try:
        sessions = [
            session for key, session in _SESSION_REGISTRY.items() if key[0] == run_id
        ]
        for key in list(_SESSION_REGISTRY):
            if key[0] == run_id:
                _SESSION_REGISTRY.pop(key, None)
    finally:
        _SESSION_LOCK.release()
    for session in sessions:
        if session.closed:
            continue
        session.closed = True
        if keep_remote and session.backend in _SESSION_OUTLIVES_HANDLE:
            # The run lifecycle still needs the node session (rubric/verify run
            # checks there) and releases it at the end: drop only this handle,
            # keep the marker so later processes and kill_run can find it.
            continue
        try:
            session.workspace.down()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            print(
                f"[warn] close_run_session: down() failed for run={run_id} "
                f"backend={session.backend}: {exc}",
                file=sys.stderr,
            )
        try:
            session_marker_path(session.run_dir, session.node_id).unlink(missing_ok=True)
        except OSError:
            pass


def install_teardown_signal_handlers(run_id: str) -> None:
    """Close ``run_id``'s sessions on SIGTERM / SIGINT, then pass the signal on.

    Each handler chains to the one installed before it, so the process reacts
    exactly as it would without a session: SIGTERM is re-delivered under
    ``SIG_DFL`` and terminates, Ctrl-C reaches Python's ``default_int_handler``
    and raises ``KeyboardInterrupt``, and a caller's own handler still runs.
    Only the main thread may install handlers; anywhere else this is a no-op
    and the run-level ``finally`` stays the teardown path.
    """
    import signal

    def _chained(prev):
        def _handler(signum, frame):
            close_run_session(run_id, lock_timeout=1.0)
            if callable(prev):
                prev(signum, frame)
            elif prev == signal.SIG_IGN:
                return
            else:  # SIG_DFL, or None (a handler installed outside Python)
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
        return _handler

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _chained(signal.getsignal(sig)))
        except (OSError, ValueError):
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