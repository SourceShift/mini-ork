"""Node-agent-backed ``Workspace`` (remote-nodes epic 06, kickoff §1).

Satisfies the :class:`mini_ork.runtime.sandbox.Workspace` protocol by driving a
remote node-agent over HTTP. The transport is pure stdlib (``urllib`` +
``http.client`` + ``json``); ``subprocess`` is only imported inside
:meth:`RemoteWorkspace.up` to run ``git rev-parse`` / ``git bundle create`` for
the one-shot engine upload.

The protocol verbs map onto the run-scoped URLs the node-agent actually ships
(re-derived from ``mini_ork/remote/node_agent/app.py`` — see the prior-art
lens's "URL authority" note; the kickoff's ``POST /v1/procs`` shorthand
resolves to ``POST /v1/sessions/{run_id}/procs``):

    POST /v1/sessions                  -> capture run_id (idempotent on body)
    PUT  /v1/engines/{sha}             -> one-shot bundle upload on sha mismatch
    POST /v1/sessions/{run_id}/procs   -> spawn; returns {pid}
    GET  /v1/sessions/{run_id}/procs/{pid}/stream -> drain out/err lines, rc
    POST /v1/sessions/{run_id}/exec    -> one-shot merged-output exec
    PUT  /v1/sessions/{run_id}/files?root=run -> tar-extracted put
    GET  /v1/sessions/{run_id}/files?root=run -> tar get
    DELETE /v1/sessions/{sid}          -> teardown

``up()`` is idempotent: a second call on the same engine sha uploads nothing.
``RemoteUnavailableError`` is raised ONLY after ``MO_REMOTE_HTTP_RETRIES``
(default 5) — never a silent host fallback. The dispatch layer maps the
exception to ``failure_class=remote_unavailable`` (kickoff §1 last clause).

Lazy import: nothing outside stdlib at module top level. The ``node``
selection lives in :mod:`mini_ork.remote.nodes` and is also imported lazily
inside ``_factory`` so the default path (``MO_SANDBOX_BACKEND`` unset) never
imports this module. Verified by ``test_remote_module_not_imported_when_backend_unset``.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import random
import sqlite3
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from mini_ork.dispatch.live_stream import LiveWriter
from mini_ork.dispatch.live_stream import live_file_path as _resolve_live_file_path
from mini_ork.runtime.sandbox import register_workspace_backend

__all__ = [
    "RemoteUnavailableError",
    "RemoteWorkspace",
    "register",
]

_TIMEOUT_RC = 124  # mirror mini_ork.remote.node_agent.procs._RC_TIMEOUT
_SPAWN_FAILED_RC = 127  # mirror mini_ork.remote.node_agent.procs._RC_SPAWN_FAILED
_KILLED_RC = 137  # 128 + SIGKILL: a proc killed before its exit code was recorded
_DEFAULT_RETRIES = 5
_DEFAULT_BACKOFF_S = 0.2  # jittered; 5 * ~0.6s = ~3s before raising
_PUT_ROOT = "run"  # node-agent's _resolve_root allowlist: run | home | mo-home

# remote-nodes-09: per-spawn pid journal ``<run_dir>/.remote-pids.jsonl``. Read by
# the web control plane's ``kill_run`` to fan a single user kill into per-pid
# ``POST /kill`` calls without a list-procs endpoint on the node-agent.
_PID_JOURNAL_NAME = ".remote-pids.jsonl"
_PID_JOURNAL_LOCK_NAME = ".remote-pids.lock"

# remote-nodes-09: client-side buffer the client tolerates beyond ``timeout_s``
# before posting ``kill`` and returning ``rc=124``. Distinct from the node-agent's
# own server-side timeout enforcement (kickoff §4).
_REMOTE_KILL_GRACE_ENV = "MO_REMOTE_KILL_GRACE_S"
_DEFAULT_KILL_GRACE_S = 30.0

# remote-nodes-10: reconnect window for the drain loop (kickoff §3).
# Default 21600 s = 6 h, long enough for a laptop sleep. The window is
# distinct from ``MO_REMOTE_KILL_GRACE_S``: the grace period is what the
# client tolerates past ``timeout_s`` while the node-agent enforces its
# own kill; the reconnect window is what the client tolerates with the
# node-agent transport gone (laptop sleep, VM up, agent down).
_REMOTE_RECONNECT_MAX_ENV = "MO_REMOTE_RECONNECT_MAX_S"
_DEFAULT_RECONNECT_MAX_S = 21600

# remote-nodes-10: capped jittered-exponential backoff for reconnect
# attempts. ``base * 2**attempt`` clamped to ``_RECONNECT_BACKOFF_CAP_S``
# (30 s) so a 6 h reconnect window makes on the order of log2(21600/0.5)
# ≈ 15 probes before giving up.
_RECONNECT_BACKOFF_BASE_S = 0.2
_RECONNECT_BACKOFF_CAP_S = 30.0

# remote-nodes-10: cadence for the offset-checkpoint write to the
# control-plane ``remote_procs`` table (kickoff §3).
_REMOTE_OFFSET_CHECKPOINT_S = 5.0

# remote-nodes-10: per-spawn sandbox env so the child claude CLI keeps its
# transcript on the run volume. ``/workspace/home/.claude`` is the
# node-agent's session-home mount; passing ``CLAUDE_CONFIG_DIR`` into the
# dispatch env (kickoff §5) makes the persisted transcript survive a
# laptop sleep — the dispatch env the agent reads becomes the run
# volume's home, not the node-agent's ephemeral ``~/.claude``.
_CLAUDE_CONFIG_DIR_KEY = "CLAUDE_CONFIG_DIR"
_CLAUDE_CONFIG_DIR_VALUE = "/workspace/home/.claude"


def _env_int_mb(name: str, default_mb: int) -> int:
    """Read ``name`` (megabytes) from env; clamp invalid to ``default_mb``.

    Mirrors the kickoff §"Size caps" table: ``MO_REMOTE_MIRROR_MAX_FILE_MB``
    (default 50) and ``MO_REMOTE_MIRROR_MAX_TOTAL_MB`` (default 500).
    Invalid values fall back to the default so a typo never widens a cap.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default_mb * 1024 * 1024
    try:
        n = int(raw)
    except ValueError:
        return default_mb * 1024 * 1024
    if n <= 0:
        return default_mb * 1024 * 1024
    return n * 1024 * 1024


class RemoteUnavailableError(RuntimeError):
    """Raised when the node-agent is unreachable after bounded retries.

    The dispatch layer maps this to ``failure_class=remote_unavailable``
    (kickoff §1). It is NEVER caught and converted to a host fallback —
    that is the entire point of the type.
    """


class _RemoteProcJournal:
    """Lazy control-plane mirror of the node-agent proc state (epic 10, §2).

    The ``remote_procs`` table is the durable state ``mini-ork recover
    --strategy reattach`` reads to decide between re-attaching to a
    still-running VM proc, harvesting an exited one, and falling back to
    ``retry``. Writes happen at three points:

      * ``upsert_start`` on the post-spawn POST → initial row + state.
      * ``upsert_offsets`` every 5 s while streaming (the reconnect loop
        checkpoints out_offset / err_offset so a hard restart still has a
        useful resume point).
      * ``upsert_end`` on terminal state (exited / timeout / spawn_failed /
        detached).

    The DB path follows the standard ``MINI_ORK_DB`` → ``$HOME/state.db``
    precedence; a missing DB is a no-op (ad-hoc dispatch path) so the
    journal never blocks a live spawn.
    """

    def __init__(self, run_dir: str | None = None, *, db_path: str | None = None) -> None:
        self._db_path = (
            db_path
            or os.environ.get("MINI_ORK_DB", "").strip()
            or _default_state_db_path(run_dir)
        )

    def _connect(self) -> sqlite3.Connection | None:
        if not self._db_path:
            return None
        try:
            con = sqlite3.connect(self._db_path, timeout=5.0)
            con.execute("PRAGMA busy_timeout=5000")
            return con
        except sqlite3.Error:
            return None

    def upsert_start(self, *, run_id: str, idempotency_key: str, node_id: str,
                     attempt: int, node_host: str, session_id: str, proc_id: int,
                     state: str, started_at: int) -> None:
        """INSERT OR REPLACE the row keyed by ``idempotency_key``."""
        con = self._connect()
        if con is None:
            return
        try:
            con.execute(
                "INSERT OR REPLACE INTO remote_procs"
                " (run_id, node_id, attempt, node_host, session_id, proc_id,"
                "  idempotency_key, state, rc, out_offset, err_offset,"
                "  started_at, ended_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, node_id, attempt, node_host, session_id,
                 proc_id, idempotency_key, state, None, 0, 0,
                 started_at, None),
            )
            con.commit()
        except sqlite3.Error:
            pass  # journal is best-effort
        finally:
            try:
                con.close()
            except sqlite3.Error:
                pass

    def upsert_offsets(self, idempotency_key: str, out_offset: int,
                       err_offset: int) -> None:
        """Update the byte-offset checkpoint for an in-flight proc."""
        con = self._connect()
        if con is None:
            return
        try:
            con.execute(
                "UPDATE remote_procs SET out_offset=?, err_offset=?"
                " WHERE idempotency_key=?",
                (int(out_offset), int(err_offset), idempotency_key),
            )
            con.commit()
        except sqlite3.Error:
            pass
        finally:
            try:
                con.close()
            except sqlite3.Error:
                pass

    def upsert_end(self, *, idempotency_key: str, state: str, rc: int | None,
                   ended_at: int) -> None:
        """Stamp the terminal state + rc + ended_at for a finished proc."""
        con = self._connect()
        if con is None:
            return
        try:
            con.execute(
                "UPDATE remote_procs SET state=?, rc=?, ended_at=?"
                " WHERE idempotency_key=?",
                (state, rc, ended_at, idempotency_key),
            )
            con.commit()
        except sqlite3.Error:
            pass
        finally:
            try:
                con.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def find_running(db_path: str, run_id: str, node_id: str) -> dict | None:
        """Return the most-recent unconsumed row for (run_id, node_id) or None.

        Used by ``mini_ork recover --strategy reattach`` to probe whether
        the proc is still alive on the VM. Pure read; no writes."""
        if not db_path or not os.path.isfile(db_path):
            return None
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            con.execute("PRAGMA busy_timeout=5000")
            try:
                row = con.execute(
                    "SELECT node_id, attempt, node_host, session_id, proc_id,"
                    " idempotency_key, state, rc, out_offset, err_offset,"
                    " started_at, ended_at"
                    " FROM remote_procs"
                    " WHERE run_id=? AND node_id=?"
                    " ORDER BY attempt DESC LIMIT 1",
                    (run_id, node_id),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return {
            "node_id": row[0], "attempt": row[1], "node_host": row[2],
            "session_id": row[3], "proc_id": row[4], "idempotency_key": row[5],
            "state": row[6], "rc": row[7], "out_offset": row[8],
            "err_offset": row[9], "started_at": row[10], "ended_at": row[11],
        }


def _default_state_db_path(run_dir: str | None) -> str:
    """Resolve the standard ``state.db`` path under ``$MINI_ORK_HOME``."""
    home = os.environ.get("MINI_ORK_HOME", "").strip()
    if not home and run_dir:
        # The run dir is ``<home>/runs/<run_id>``; climb two to get home.
        home = str(Path(run_dir).parent.parent)
    if not home:
        return ""
    return os.path.join(home, "state.db")


class _RedactingLiveWriter:
    """A :class:`LiveWriter` whose lines are secret-masked before they land."""

    def __init__(self, inner: LiveWriter, redactor: Any) -> None:
        self._inner = inner
        self._redactor = redactor

    def write_line(self, data: str, stream: str, *args: Any, **kwargs: Any) -> Any:
        return self._inner.write_line(self._redactor.redact_text(data), stream, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _NodeRef(NamedTuple):
    """The minimal slice of :class:`mini_ork.remote.nodes.Node` this module needs.

    Defined here (rather than imported from ``nodes.py``) so the module's
    import-time surface stays stdlib-only. ``nodes.select_node`` returns a
    ``Node`` whose first 4 fields match this shape; the factory coerces via
    ``_NodeRef(**node._asdict())`` if a Node is passed.
    """

    name: str
    url: str
    token: str
    max_sessions: int = 1


class RemoteWorkspace:
    """A ``Workspace`` whose verbs drive a remote node-agent over HTTP.

    The session lifecycle mirrors :class:`DockerWorkspace`: ``up()`` is called
    once per run (idempotent on engine sha), ``spawn``/``exec`` are warm, and
    ``down()`` ``DELETE``-s the session. ``put``/``get`` round-trip via the
    node-agent's ``/v1/sessions/{run_id}/files?root=run`` tar endpoints and
    return an IN-WORKSPACE path (e.g. ``run/put-<hex>.txt``) — the same shape
    :class:`DockerWorkspace` returns at ``<mount_path>/put-*.txt``.
    """

    def __init__(
        self,
        *,
        node: _NodeRef,
        run_id: str,
        image: str,
        drive_root: str,
        mount_path: str = "/workspace",
        engine_root: str | None = None,
        retries: int | None = None,
        token_env: str | None = None,
        on_chunk: Any = None,
        target_root: str | None = None,
        run_dir: str | None = None,
        profile: Any = None,
        queue_wait_s: float | None = None,
    ) -> None:
        # No isinstance guard on `node` — the typed signature already constrains
        # callers to ``_NodeRef`` (the factory coerces ``Node`` to ``_NodeRef``),
        # and an explicit check would be flagged as unreachable by type checkers
        # while adding no runtime safety beyond what the type promises.
        if not run_id:
            raise ValueError("RemoteWorkspace requires a non-empty run_id")
        if not image:
            raise ValueError("RemoteWorkspace requires a non-empty image")
        self._node = node
        self._run_id = run_id
        self._image = image
        # remote-nodes-14: the environment profile (epic 12) binds the session's
        # prepared image, resources and network; None keeps today's payload.
        self._profile = profile
        self._base_image = image   # _image becomes the prepared tag after image_prepare
        self._queue_wait_s = float(600.0 if queue_wait_s is None else queue_wait_s)
        self._sync_up_bytes = 0
        self._sync_down_bytes = 0
        self._setup_s: float | None = None
        self._up_at: float | None = None
        # Optional observer of every remote.setup.step record (``nodes doctor``).
        self.on_setup_step: Any = None
        self._drive_root = drive_root
        self._mount_path = mount_path
        # engine_root defaults to $MINI_ORK_ROOT (the control plane checkout);
        # tests override to a tmp dir so the dirty-tree refuse rule is local.
        self._engine_root = engine_root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
        self._retries = int(retries if retries is not None else os.environ.get(
            "MO_REMOTE_HTTP_RETRIES", _DEFAULT_RETRIES
        ))
        # token_env is a NAME only — the actual token is read at call time so a
        # rotation does not require a restart, and so a process restart does not
        # pin a stale value (the MINI_ORK_SECRETS_FOR_FOREIGN_HOME lesson).
        self._token_env = token_env or "MO_NODE_TOKEN"
        self._on_chunk = on_chunk or (lambda: None)
        self._sid: str | None = None  # populated from POST /v1/sessions response
        self._uploaded_shas: set[str] = set()  # idempotent engine upload
        self._owns_session: bool = False  # False until up() succeeds
        # Epic 07: one ``last_synced`` per session, not per spawn
        # (kickoff §2). All sync operations are serialized by the lock
        # so a parallel-pool pair of spawns cannot race.
        self._last_synced: tuple[str, str] | None = None  # (commit, tree)
        # One re-entrant lock for every session-state change: tree sync (07)
        # and the run-dir mirror (08) must never interleave, and the mirror
        # helpers take it themselves, possibly inside a sync block.
        self._sync_lock = threading.RLock()
        self._session_lock = self._sync_lock
        # Constructor-injected target_root (tests); production resolves the
        # run's pinned roots in ``_resolve_target_root``.
        self._target_root: str | None = target_root
        # Run-dir mirror state (kickoff 08). The lock is shared with the
        # future tree-sync path (epic 07) so a mirror push/pull never
        # interleaves with a tree bundle upload on the same session.
        self._mo_home_uploaded: bool = False
        # Captured at mirror_push() time; consulted at mirror_pull() time to
        # detect local-vs-remote conflicts (the control plane wins).
        self._mirror_push_snap: dict[str, str] = {}
        # The local run dir. When unset (existing tests, the conformance
        # suite) the mirror hooks short-circuit; the production path passes
        # it explicitly via the resolver/factory so the agents that read
        # ${MINI_ORK_RUN_DIR} see the same files locally and remotely.
        self._run_dir: str | None = run_dir or os.environ.get("MINI_ORK_RUN_DIR")
        # remote-nodes-09: per-spawn pid journal location. Resolved lazily
        # so a workspace constructed before ``MINI_ORK_RUN_DIR`` is set
        # (e.g. an ad-hoc dispatch from the CLI) doesn't crash on import.
        self._pid_journal: Path | None = None
        self._pid_journal_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _idempotency_key(self, env: Mapping[str, str]) -> str:
        """Compute the dedup key the node-agent uses to re-attach by construction.

        ``sha256(run_id|node_id|attempt|input_hash)`` mirrors the durable-DAG
        input hash (``mini_ork/stores/checkpoints.py``) so a re-dispatch of
        the same attempt — same argv, same upstream inputs — resolves to the
        existing proc instead of paying for a second VM spawn. The two env
        contracts (``MO_NODE_ATTEMPT``, ``MO_INPUT_HASH``) are populated by
        the dispatch layer; when absent, the key is empty and the node-agent
        falls back to its legacy pid-sequenced spawn (no behavior change).
        """
        node_id = (env.get("MO_NODE_ID", "") if isinstance(env, Mapping) else "").strip()
        attempt = (env.get("MO_NODE_ATTEMPT", "") if isinstance(env, Mapping) else "").strip()
        input_hash = (env.get("MO_INPUT_HASH", "") if isinstance(env, Mapping) else "").strip()
        if not node_id or not attempt or not input_hash:
            return ""
        import hashlib as _hashlib
        return _hashlib.sha256(
            f"{self._run_id}|{node_id}|{attempt}|{input_hash}".encode()
        ).hexdigest()

    def _token(self) -> str:
        """Read the bearer token at call time (never at import); fall back to
        the token ``select_node`` resolved from the node's ``token_env``."""
        return os.environ.get(self._token_env, "") or self._node.token

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        query: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        retries: int | None = None,
    ) -> bytes:
        """Issue an authenticated HTTP request with bounded retries + jitter.

        Returns the response body. Raises :class:`RemoteUnavailableError` after
        ``_retries`` exhausted transport errors. 5xx + transport errors retry;
        4xx is treated as a programming error and re-raised as
        :class:`RemoteUnavailableError` immediately (a 401/403 means the token
        is wrong — retrying just lengthens the outage).
        """
        url = self._node.url.rstrip("/") + path
        if query:
            from urllib.parse import urlencode

            url += "?" + urlencode(query)
        last_exc: Exception | None = None
        n_tries = max(1, int(retries if retries is not None else self._retries))
        for attempt in range(n_tries):
            req = urllib_request.Request(url, data=body, method=method)
            if self._token():
                req.add_header("Authorization", f"Bearer {self._token()}")
            if content_type:
                req.add_header("Content-Type", content_type)
            try:
                with urllib_request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except urllib_error.HTTPError as exc:
                # 5xx: transient, retry. 4xx: caller error (token, payload),
                # fail loud NOW.
                if 500 <= exc.code < 600 and attempt + 1 < n_tries:
                    last_exc = exc
                    time.sleep(_DEFAULT_BACKOFF_S * (1 + random.random()))
                    continue
                raise RemoteUnavailableError(
                    f"node-agent {method} {path} -> HTTP {exc.code}: "
                    f"{exc.reason}"
                ) from exc
            except (urllib_error.URLError, OSError) as exc:
                # Connection refused / DNS / timeout — retry with jittered backoff.
                last_exc = exc
                if attempt + 1 < n_tries:
                    time.sleep(_DEFAULT_BACKOFF_S * (1 + random.random()))
                    continue
                raise RemoteUnavailableError(
                    f"node-agent {method} {path} unreachable after "
                    f"{n_tries} attempts: {exc}"
                ) from exc
        # Loop fell off without raising — defensive.
        raise RemoteUnavailableError(
            f"node-agent {method} {path} failed: {last_exc}"
        )

    def _json(self, method: str, path: str, payload: Mapping[str, Any] | None = None,
              **request_kw: Any) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        raw = self._request(method, path, body=body, content_type="application/json",
                            **request_kw)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------ Workspace

    def up(self) -> None:
        """Provision the remote workspace: stage engine, create session.

        Idempotent on engine sha: a second ``up()`` for a sha we've already
        uploaded is a no-op for the engine stage and returns the same sid.
        A dirty ``$MINI_ORK_ROOT`` is refused UNLESS
        ``MO_REMOTE_ALLOW_DIRTY_ENGINE=1`` — a sha must name an exact tree.
        ``subprocess`` is imported here (NOT at module top level) because the
        git bundle is the only subprocess call this module makes.
        """
        # Lazy import: subprocess is the one stdlib module we do not want at
        # module-import time (gradient gr-1c9d2fad50a1: minimize top-level
        # surface so the module is importable from a frozen/embedded build
        # without forking). The git binary itself is found on demand.
        import subprocess

        t_up = time.monotonic()
        # 1) Engine sha stage (idempotent).
        sha = self._current_engine_sha(subprocess)
        self._engine_sha = sha
        creating = self._sid is None
        health: dict = {}
        if sha not in self._uploaded_shas or creating:
            # Health doubles as the capacity gate (remote-nodes-14 §5), checked
            # only while this run holds no session on the node.
            health = self._setup_step(
                "health", self._await_capacity if creating else (lambda: self._json("GET", "/v1/health")))
        if sha not in self._uploaded_shas:
            def _stage_engine() -> None:
                remote_shas = {entry["sha"] for entry in health.get("engine_shas", [])}
                if sha not in remote_shas:
                    self._request("PUT", f"/v1/engines/{sha}", body=self._bundle_engine(subprocess),
                                  content_type="application/x-git-bundle")
            self._setup_step("engine", _stage_engine, detail=sha[:12])
            self._uploaded_shas.add(sha)

        # 2) Session create (idempotent on run_id at the server), on the
        # profile's prepared image when the profile declares a setup script.
        if creating:
            if self._profile is not None and (getattr(self._profile, "setup", "") or "").strip():
                self._image = self._setup_step("image_prepare", self._prepare_image)
            sess = self._setup_step("session_up", lambda: self._json(
                "POST", "/v1/sessions", self._session_payload()))
            self._sid = sess.get("sid", self._run_id)
            self._owns_session = True
            self._load_sync_state()
        # 3) Initial tree sync-up (epic 07) and the one-shot mo-home upload
        # (epic 08). Both are idempotent on a repeated up().
        with self._sync_lock:
            self._setup_step("initial_sync", lambda: self._sync_up(force=True))
        self._setup_step("post_sync", self._upload_mo_home)
        if creating:
            self._up_at = time.monotonic()
            self._setup_s = self._up_at - t_up
        return None

    # ------------------------------------------------------------------ provisioning

    # Every dispatch and check of a run runs in its own process, each with its
    # own workspace object. The replica's sync base lives in the run dir so a
    # new process syncs incrementally (usually: nothing) instead of shipping
    # the whole tree again — valid only while the node holds the SAME session.
    _SYNC_STATE = ".remote-sync-state.json"

    def _sync_state_path(self) -> Path | None:
        return Path(self._run_dir) / self._SYNC_STATE if self._run_dir else None

    def _load_sync_state(self) -> None:
        path = self._sync_state_path()
        if path is None or self._last_synced is not None or not path.is_file():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if state.get("run_id") == self._run_id and state.get("sid") == self._sid \
                and state.get("commit") and state.get("tree"):
            self._last_synced = (str(state["commit"]), str(state["tree"]))

    def _save_sync_state(self) -> None:
        path = self._sync_state_path()
        if path is None or self._last_synced is None:
            return
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"run_id": self._run_id, "sid": self._sid,
                                       "commit": self._last_synced[0],
                                       "tree": self._last_synced[1]}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def _setup_step(self, step: str, fn: Any, *, detail: str = "") -> Any:
        """Run one provisioning step between ``remote.setup.step`` start/ok|fail
        events (epic 12 §4) — the data for a live "setting up" checklist."""
        self._setup_event(step=step, status="start")
        t0 = time.monotonic()
        try:
            out = fn()
        except Exception as exc:
            self._setup_event(step=step, status="fail", ms=int((time.monotonic() - t0) * 1000),
                              detail=str(exc)[:500])
            raise
        self._setup_event(step=step, status="ok", ms=int((time.monotonic() - t0) * 1000),
                          detail=detail)
        return out

    def _setup_event(self, **fields: Any) -> None:
        self._emit_sync_event("remote.setup.step", **fields)
        if self.on_setup_step is not None:
            try:
                self.on_setup_step(dict(fields))
            except Exception:  # noqa: BLE001 — an observer never breaks provisioning
                pass

    def _await_capacity(self) -> dict:
        """Wait for a free session slot on the node; return its health.

        A node runs at most ``max_sessions`` sessions (``nodes.yaml``). When it
        is full, poll with backoff for up to ``MO_REMOTE_QUEUE_WAIT_S``
        (emitting ``remote.queue.wait``), then fail ``remote_unavailable``. A
        run that already holds a session there (a control-plane restart) is
        never queued behind itself."""
        cap = max(1, int(getattr(self._node, "max_sessions", 1) or 1))
        deadline = time.monotonic() + self._queue_wait_s
        backoff = 1.0
        while True:
            health = self._json("GET", "/v1/health")
            live = int(health.get("sessions") or 0)
            if live < cap or self._session_exists():
                return health
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteUnavailableError(
                    f"remote_unavailable: node {self._node.name or self._node.url!r} is at capacity "
                    f"({live}/{cap} sessions); waited {self._queue_wait_s:.0f}s "
                    "(MO_REMOTE_QUEUE_WAIT_S)")
            pause = min(backoff, remaining)
            self._emit_sync_event("remote.queue.wait", node=self._node.name, live=live, cap=cap,
                                  wait_s=round(pause, 2))
            time.sleep(pause)
            backoff = min(backoff * 2, 15.0)

    def _session_exists(self) -> bool:
        try:
            self._json("GET", f"/v1/sessions/{self._run_id}", retries=1)
            return True
        except RemoteUnavailableError:
            return False

    def _prepare_image(self) -> str:
        """Build (or hit the cache for) the profile's setup image on the node."""
        prof = self._profile
        timeout_s = float(getattr(prof, "setup_timeout_s", 900.0) or 900.0)
        body = self._json("POST", "/v1/images/prepare",
                          {"base_image": self._base_image, "setup": prof.setup,
                           "timeout_s": timeout_s, "run_id": self._run_id},
                          timeout=timeout_s + 60, retries=1)   # a failed setup is not transient
        tag = str(body.get("tag") or "")
        if not tag:
            raise RemoteUnavailableError(
                f"image prepare for profile {getattr(prof, 'name', '')!r} returned no tag: "
                f"{str(body.get('log_tail') or '')[-500:]}")
        return tag

    def _session_payload(self) -> dict:
        payload: dict[str, Any] = {"run_id": self._run_id, "image": self._image}
        if getattr(self, "_engine_sha", ""):
            payload["engine_sha"] = self._engine_sha   # D8: mount THIS engine at /opt/mini-ork
        prof = self._profile
        if prof is not None:
            if getattr(prof, "resources", None):
                payload["resources"] = dict(prof.resources)
            payload["network"] = getattr(prof, "network", "") or "full"
            if getattr(prof, "allow_domains", None):
                payload["allow_domains"] = list(prof.allow_domains)
            if payload["network"] == "allowlist":
                # remote-nodes-13 §3: the proxy must admit the lanes' own
                # endpoints (base_url hosts, else provider defaults), or every
                # LLM call of an allowlist session is blocked.
                from mini_ork.dispatch.providers import _load_providers_registry
                from mini_ork.remote.secrets_scope import lane_egress_hosts

                domains = list(payload.get("allow_domains") or [])
                domains += [h for h in lane_egress_hosts(_load_providers_registry(self._engine_root))
                            if h not in domains]
                payload["allow_domains"] = domains
        return payload

    def _current_engine_sha(self, subprocess_mod: Any) -> str:
        """Return ``git rev-parse HEAD`` for ``engine_root``; refuse dirty trees.

        ``subprocess_mod`` is the imported :mod:`subprocess` module — passed
        in so ``up()`` owns the only import. Dirty-tree bypass:
        ``MO_REMOTE_ALLOW_DIRTY_ENGINE=1``.
        """
        # git -C <root> rev-parse HEAD
        r = subprocess_mod.run(
            ["git", "-C", self._engine_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if r.returncode != 0:
            raise RemoteUnavailableError(
                f"cannot read engine sha at {self._engine_root!r}: "
                f"{r.stderr.strip() or r.stdout.strip()}"
            )
        sha = r.stdout.strip()
        if not os.environ.get("MO_REMOTE_ALLOW_DIRTY_ENGINE"):
            d = subprocess_mod.run(
                ["git", "-C", self._engine_root, "diff", "--quiet"],
                capture_output=True, text=True, check=False, timeout=15,
            )
            if d.returncode != 0:
                raise RemoteUnavailableError(
                    f"engine tree at {self._engine_root!r} is dirty; a sha "
                    "must name an exact tree. Set MO_REMOTE_ALLOW_DIRTY_ENGINE=1 "
                    "to override (testing only)."
                )
        return sha

    def _bundle_engine(self, subprocess_mod: Any) -> bytes:
        """A self-contained git bundle of the engine tree at HEAD.

        One parentless commit of HEAD's tree under ``refs/mo/engine``: no
        history to ship, and no prerequisite commits — a bundle of HEAD from a
        shallow clone (CI) names the missing parent as a prerequisite, which
        a node with no copy of the repo cannot satisfy."""
        root = self._engine_root
        env = {**os.environ, "GIT_AUTHOR_NAME": "mini-ork", "GIT_AUTHOR_EMAIL": "engine@mini-ork",
               "GIT_COMMITTER_NAME": "mini-ork", "GIT_COMMITTER_EMAIL": "engine@mini-ork"}

        def git(*args: str) -> str:
            r = subprocess_mod.run(["git", "-C", root, *args], capture_output=True, text=True,
                                   check=False, timeout=300, env=env)
            if r.returncode != 0:
                raise RemoteUnavailableError(
                    f"engine bundle: git {' '.join(args)} failed in {root!r}: "
                    f"{r.stderr.strip() or r.stdout.strip()}")
            return r.stdout.strip()

        with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as tmp:
            tmp_path = tmp.name
        ref = f"refs/mo/engine-bundle-{os.getpid()}"
        try:
            commit = git("commit-tree", git("rev-parse", "HEAD^{tree}"),
                         "-m", f"mini-ork engine {git('rev-parse', 'HEAD')}")
            git("update-ref", ref, commit)
            git("bundle", "create", tmp_path, ref)
            with open(tmp_path, "rb") as fh:
                return fh.read()
        finally:
            subprocess_mod.run(["git", "-C", root, "update-ref", "-d", ref],
                               capture_output=True, check=False, timeout=60)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------ epic 07: tree sync
    #
    # The kickoff (remote-nodes-07 §2) splits the sync lifecycle into
    # three hooks:
    #   up()    -> force=True initial sync-up (always).
    #   spawn() -> sync_up if local tree differs from _last_synced,
    #              then run the proc, then sync_down (always).
    #   exec()  -> sync_up only. NEVER sync-down after exec (verifiers
    #              and tool exec do not write the truth; epic 11).
    #
    # A single ``_last_synced: (commit, tree)`` per session (NOT per
    # spawn) is what makes parallel-pool correct: peer A's sync-down
    # updates ``_last_synced``; peer B's sync-up then sees a delta
    # only, not a fresh full upload.
    #
    # ``tree_sync`` is imported lazily to preserve the default-path
    # property (``MO_SANDBOX_BACKEND`` unset never imports this module
    # — verified by ``test_remote_module_not_imported_when_backend_unset``).

    def _resolve_target_root(self) -> str:
        """The local target checkout: the run's pinned roots (remote-nodes-01),
        else an explicit ``MO_TARGET_CWD``. Never the engine checkout — syncing
        mini-ork's own tree as the target is exactly the mistake to rule out."""
        if self._target_root is not None:
            return self._target_root
        root = ""
        run_dir = (self._run_dir or os.environ.get("MINI_ORK_RUN_DIR") or "").strip()
        if run_dir:
            from mini_ork.runtime.run_roots import load_run_roots

            roots = load_run_roots(run_dir)
            root = roots.target if roots else ""
        root = root or (os.environ.get("MO_TARGET_CWD") or "").strip()
        if not root or not (Path(root) / ".git").exists():
            raise RemoteUnavailableError(
                f"no git target to sync (pinned roots / MO_TARGET_CWD gave {root!r}); "
                "a remote session needs the run's target checkout"
            )
        self._target_root = root
        return root

    def _current_target_tree(self, ts: Any) -> str:
        return ts.worktree_tree(self._resolve_target_root())

    def _sync_up(self, *, force: bool = False) -> None:
        """Bring the replica to the local worktree. First time: an initial
        bundle (full -> branch -> squashed). Afterwards: only what is new since
        ``_last_synced``, and only when the local tree actually changed."""
        from mini_ork.remote import tree_sync as _ts  # lazy (default-path rule)

        target = self._resolve_target_root()
        if (not force and self._last_synced is not None
                and self._current_target_tree(_ts) == self._last_synced[1]):
            return
        snap = _ts.snapshot(target, parent="HEAD")
        if self._last_synced is not None and snap.tree == self._last_synced[1]:
            return   # the replica already holds this exact tree (e.g. a repeated up())
        if self._last_synced is None:
            bundle_path, mode = _ts.initial_bundle(target, snap)
        else:
            bundle_path, mode = (_ts.incremental_bundle(target, new=snap.commit,
                                                        base=self._last_synced[0]), "incremental")
            if bundle_path is None:
                return
        tip = _ts.bundle_tip(target)   # == snap.commit, or the orphan when squashed
        # The replica detaches at the local HEAD so its `git status` matches ours;
        # a squashed upload carries no history, so it detaches at the orphan.
        head = tip if mode == "squashed" else _ts._out(_ts._git(target, "rev-parse", "HEAD"))
        try:
            size = bundle_path.stat().st_size
            self._request("POST", f"/v1/sessions/{self._run_id}/tree/bundle",
                          body=bundle_path.read_bytes(), content_type="application/x-git-bundle")
            self._json("POST", f"/v1/sessions/{self._run_id}/tree/materialize",
                       payload={"snap": {"commit": tip, "tree": snap.tree,
                                         "excluded": list(snap.excluded)},
                                "head": head})
        finally:
            bundle_path.unlink(missing_ok=True)
        self._last_synced = (tip, snap.tree)
        self._save_sync_state()
        if snap.excluded:
            self._emit_sync_event("remote.sync.excluded", names=list(snap.excluded))
        self._emit_sync_event("remote.sync.up", bytes=size, tree=snap.tree, mode=mode)

    def _sync_down(self) -> None:
        """Pull the replica's edits into the local checkout as uncommitted changes.

        ``SyncConflictError`` (the local checkout changed meanwhile) propagates;
        the remote snapshot stays fetchable at ``refs/mo/remote/<run>/latest``."""
        import base64

        from mini_ork.remote import tree_sync as _ts  # lazy

        if self._last_synced is None:
            raise RemoteUnavailableError("sync_down before sync_up: nothing to diff against")
        target = self._resolve_target_root()
        body = self._json("POST", f"/v1/sessions/{self._run_id}/tree/snapshot",
                          payload={"base": self._last_synced[0]})
        new_snap = _ts.Snap(commit=body["commit"], tree=body["tree"],
                            excluded=tuple(body.get("excluded") or ()))
        base_snap = _ts.Snap(commit=self._last_synced[0], tree=self._last_synced[1])
        data = base64.b64decode(body.get("bundle_b64") or "")
        ref = f"refs/mo/remote/{self._run_id}/latest"
        if data:   # empty: nothing new remotely, the objects are already here
            fd, tmp = tempfile.mkstemp(prefix="mo-sync-down-", suffix=".bundle")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                _ts.fetch_bundle(target, tmp, ref)
            finally:
                os.unlink(tmp)
        head_moved = _ts.apply_delta(target, base_snap, new_snap, ref=ref)
        self._last_synced = (new_snap.commit, new_snap.tree)
        self._save_sync_state()
        if head_moved:
            self._emit_sync_event("remote.head_moved", tree=new_snap.tree)
        self._emit_sync_event("remote.sync.down", bytes=len(data), tree=new_snap.tree)

    def restore_replica(self) -> None:
        """Restore the replica's non-ignored paths to ``_last_synced`` (kickoff §4).

        Called by every ``run_check`` exec so verifier droppings and mutation
        residue can never be mistaken for agent edits on the next sync-down.
        The remote node-agent restores the tree from the session's
        ``last_synced`` snapshot, then ``git clean -fd`` (NO ``-x``) so
        ignored caches like ``.venv``, ``node_modules``, and
        ``.pytest_cache`` survive for speed.

        No-op when ``_last_synced`` is unset (legacy / un-preceded session):
        there is nothing to restore against. Errors propagate as
        ``RemoteUnavailableError`` so the caller can decide whether to
        mask them (the contract: best-effort hygiene; truth is the check).
        """
        if self._last_synced is None:
            return
        self._json(
            "POST",
            f"/v1/sessions/{self._run_id}/tree/restore",
            {"base_commit": self._last_synced[0]},
        )

    def _emit_sync_event(self, event_type: str, **fields: Any) -> None:
        """Fire a ``remote.sync.*`` event through the run-event emitter.

        Silent if the emitter is unavailable (no DB in scope). The
        payload is what the kickoff §5 demands: ``bytes``,
        ``files_changed``, ``ms``, ``tree``. ``files_changed`` and
        ``ms`` are best-effort — we don't compute them here, callers
        may override via ``fields``.
        """
        if event_type == "remote.sync.up":
            self._sync_up_bytes += int(fields.get("bytes") or 0)
        elif event_type == "remote.sync.down":
            self._sync_down_bytes += int(fields.get("bytes") or 0)
        run_id = os.environ.get("MINI_ORK_RUN_ID", self._run_id)
        try:
            from mini_ork.observability.node_events import mo_node_emit
            mo_node_emit(
                run_id, node_id="remote-workspace", node_type="workspace",
                event_type=event_type, extra_json=json.dumps(fields),
            )
        except Exception:  # emitter must never break the sync
            pass

    # ------------------------------------------------------------------ workspace

    def exec(self, cmd: str, *, cwd: str, timeout: int,
             env: Mapping[str, str] | None = None) -> tuple[int, str]:
        if self._sid is None:
            raise RuntimeError("RemoteWorkspace.exec called before up()")
        # Pre: tree sync-up (epic 07) + run-dir push (epic 08). Post: run-dir
        # pull only — a check never syncs the tree back (it is not the truth).
        with self._sync_lock:
            self._sync_up(force=False)
        self.mirror_push()
        try:
            # Use /v1/sessions/{run_id}/exec — argv=[sh,-c,cmd] so the server-side
            # argv-only contract is preserved (no shell interpolation by the
            # node-agent). The endpoint merges streams (its rc/output shape).
            result = self._json(
                "POST",
                f"/v1/sessions/{self._run_id}/exec",
                {
                    "argv": ["sh", "-c", cmd],
                    "cwd": cwd or self._mount_path,
                    "timeout_s": timeout,
                    # A check's env (plan/artifact/run-dir paths) — values ride
                    # the request; the node-agent persists keys only.
                    **({"env": dict(env)} if env else {}),
                },
            )
            rc = int(result.get("rc", _SPAWN_FAILED_RC))
            out = str(result.get("output", ""))
        finally:
            self.mirror_pull()
        return rc, out

    def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        timeout: float,
        env: Mapping[str, str],
        cwd: str | None,
        live_file_path: str = "",
    ) -> tuple[int, str, str]:
        if self._sid is None:
            raise RuntimeError("RemoteWorkspace.spawn called before up()")
        argv_list = [str(a) for a in argv]
        if not argv_list:
            raise ValueError("spawn requires a non-empty argv")
        # remote-nodes-09: tee to the per-node live sidecar while the proc runs.
        # The dispatch layer passes the host path; MO_LIVE_FILE is the fallback.
        live_path = live_file_path or _resolve_live_file_path()
        live = LiveWriter(live_path) if live_path else None
        if live is not None and isinstance(env, Mapping):
            # remote-nodes-13 §2, defense in depth: the node-agent already
            # redacts, and the host-side live file is masked again.
            from mini_ork.remote.secrets_scope import make_redactor, secret_values

            live = _RedactingLiveWriter(live, make_redactor(secret_values(env)))
        node_id = (env.get("MO_NODE_ID", "") if isinstance(env, Mapping) else "").strip()
        # remote-nodes-10: idempotency dedup + transcript persistence. The
        # dispatch layer sets MO_NODE_ATTEMPT + MO_INPUT_HASH from the
        # durable-DAG checkpoint; together with MO_NODE_ID they identify
        # the (run, node, attempt, inputs) tuple. The node-agent keys on
        # the same hash so a re-dispatch re-attaches to the existing proc.
        idem_key = self._idempotency_key(env)
        # remote-nodes-10: pin the child's claude config dir to the run
        # volume so the transcript persists across laptop sleep. Inherits
        # any caller-provided value (test override) before applying the
        # default; the env-keys set is unioned so the node-agent persists
        # the new key.
        spawn_env = dict(env)
        if self._run_dir and not spawn_env.get(_CLAUDE_CONFIG_DIR_KEY):
            spawn_env[_CLAUDE_CONFIG_DIR_KEY] = _CLAUDE_CONFIG_DIR_VALUE
        env_keys = sorted(set(env.keys()) | set(spawn_env.keys()))
        self._emit_proc_event("remote.proc.start", node=node_id, proc_id="pending",
                              node_host=self._node.name)
        start_ms = int(time.time() * 1000)
        rc = _SPAWN_FAILED_RC
        journal = _RemoteProcJournal(self._run_dir) if idem_key else None \
            # only journal when we have a stable identity
        try:
            # Pre: tree sync-up (epic 07) + run-dir push (epic 08). Only the sync
            # steps hold the session lock, so parallel spawns on one session still
            # run concurrently.
            with self._sync_lock:
                self._sync_up(force=False)
            self.mirror_push()
            try:
                # 1) POST .../procs — stdin goes once in the body; env values ride
                # the request (never argv) and the node-agent persists keys only.
                payload = {
                    "argv": argv_list,
                    "cwd": cwd or self._mount_path,
                    "env": spawn_env,
                    "env_keys": env_keys,
                    "stdin": stdin,
                    "timeout_s": timeout,
                }
                if idem_key:
                    payload["idempotency_key"] = idem_key
                proc = self._json(
                    "POST",
                    f"/v1/sessions/{self._run_id}/procs",
                    payload,
                )
                pid = int(proc["pid"])
                # The pid journal lets kill_run fan a kill out per pid (the
                # node-agent has no list-procs endpoint).
                self._record_pid(pid, node_id=node_id, argv0=argv_list[0] if argv_list else "")
                # remote-nodes-10: write the initial remote_procs row so the
                # control plane knows the proc is alive (recovery uses this
                # to default to strategy=reattach).
                if journal is not None and idem_key:
                    attempt_str = (env.get("MO_NODE_ATTEMPT", "") if isinstance(env, Mapping) else "").strip() or "1"
                    try:
                        attempt_int = int(attempt_str)
                    except ValueError:
                        attempt_int = 1
                    journal.upsert_start(
                        run_id=self._run_id,
                        idempotency_key=idem_key,
                        node_id=node_id,
                        attempt=attempt_int,
                        node_host=self._node.name,
                        session_id=self._sid or self._run_id,
                        proc_id=pid,
                        state=str(proc.get("state", "running")),
                        started_at=int(time.time()),
                    )
                # 2) Drain the JSON-lines stream (out/err kept separate), teeing live.
                # The reconnect loop is bounded by ``MO_REMOTE_RECONNECT_MAX_S``
                # (default 6 h) so a laptop sleep does not surface as a failed
                # node — the client re-attaches from the persisted byte offsets.
                rc, out, err = self._drain_stream(
                    pid, timeout, live_writer=live, journal=journal,
                    idempotency_key=idem_key, node_id=node_id,
                )
                # Post: the agent's tree edits come back as uncommitted changes.
                with self._sync_lock:
                    self._sync_down()
            finally:
                self.mirror_pull()   # the run dir comes back even on a sync conflict
                if journal is not None and idem_key:
                    journal.upsert_end(
                        idempotency_key=idem_key,
                        state=self._state_for_rc(rc),
                        rc=rc,
                        ended_at=int(time.time()),
                    )
            return rc, out, err
        finally:
            self._emit_proc_event("remote.proc.exit", node=node_id, rc=rc,
                                  ms=int(time.time() * 1000) - start_ms, killed=False)
            if live is not None:
                live.close()

    @staticmethod
    def _state_for_rc(rc: int) -> str:
        """Map a return code to a ``remote_procs`` state string."""
        if rc == _TIMEOUT_RC:
            return "timeout"
        if rc == _SPAWN_FAILED_RC:
            return "spawn_failed"
        return "exited"

    def _drain_stream(
        self,
        pid: int,
        timeout: float,
        *,
        live_writer: LiveWriter | None = None,
        journal: "_RemoteProcJournal | None" = None,
        idempotency_key: str = "",
        node_id: str = "",
    ) -> tuple[int, str, str]:
        """Drain the proc stream endpoint until exit line; return (rc, out, err).

        Streams are kept separate (the spawn contract requires it; merging
        would corrupt the provider's JSON envelope on stdout). The node-agent
        also reports ``state="timeout"`` on its own timeout — we mirror that
        as the conventional ``rc=124`` with an empty stdout + stderr message.

        ``live_writer`` (remote-nodes-09): when provided, every out/err chunk
        is teed through ``LiveWriter.write_line`` so a tailer polling the
        live sidecar sees each line as it arrives, not at exit.

        Reconnect loop (remote-nodes-10): when the node-agent transport
        goes away mid-drain — laptop sleep, control plane down — the
        client emits ``remote.proc.detached``, polls ``GET /procs/{pid}``
        for state + offsets, and re-enters the stream with
        ``?out=<o>&err=<e>`` so no byte is duplicated or lost. After
        ``MO_REMOTE_RECONNECT_MAX_S`` (default 6 h) the client gives up,
        posts ``kill``, and returns ``rc=124`` with ``infra_interruption``
        surfaced through ``remote.proc.lost``. The cap-jittered-exp
        backoff stays well below the deadline so a long sleep still
        makes progress. ``journal`` writes offsets every 5 s while
        streaming so the control plane can pick up where we left off
        even on a hard restart."""
        out = io.StringIO()
        err = io.StringIO()
        out_off = 0  # byte offset of next expected stdout chunk (utf-8 encoded)
        err_off = 0  # byte offset of next expected stderr chunk
        # remote-nodes-09 §4: client-side deadline is ``timeout_s`` PLUS
        # ``MO_REMOTE_KILL_GRACE_S`` (default 30). On expiry we post ``kill``
        # and return rc=124, so the client never waits unbounded on a remote
        # process — the kickoff's hard constraint.
        grace_s = _DEFAULT_KILL_GRACE_S
        raw_grace = os.environ.get(_REMOTE_KILL_GRACE_ENV, "").strip()
        if raw_grace:
            try:
                grace_s = max(0.0, float(raw_grace))
            except ValueError:
                grace_s = _DEFAULT_KILL_GRACE_S
        timeout_deadline = time.time() + timeout + grace_s
        # remote-nodes-10: separate reconnect window. Default 6 h, long enough
        # for a laptop sleep; tunable via ``MO_REMOTE_RECONNECT_MAX_S``.
        raw_max = os.environ.get(_REMOTE_RECONNECT_MAX_ENV, "").strip()
        try:
            reconnect_max_s = float(raw_max) if raw_max else float(_DEFAULT_RECONNECT_MAX_S)
        except ValueError:
            reconnect_max_s = float(_DEFAULT_RECONNECT_MAX_S)
        reconnect_deadline = time.time() + max(0.0, reconnect_max_s)
        backoff_s = _RECONNECT_BACKOFF_BASE_S
        last_offset_checkpoint = time.time()
        last_rc: int = 0
        last_state: str = ""
        was_detached = False  # emit detached at most once per drop, reattached on resume
        while True:
            try:
                chunk = self._request(
                    "GET",
                    f"/v1/sessions/{self._run_id}/procs/{pid}/stream",
                    query={"out": str(out_off), "err": str(err_off)},
                )
            except (RemoteUnavailableError, OSError, http.client.HTTPException):
                # Transport died mid-drain (a drop mid-response surfaces as
                # URLError / ConnectionResetError / IncompleteRead, not only as
                # RemoteUnavailableError). Past the reconnect window → fail loud.
                if time.time() >= reconnect_deadline:
                    self._kill_proc(pid)
                    self._emit_proc_event(
                        "remote.proc.lost",
                        node=node_id, proc_id=str(pid),
                        failure_class="infra_interruption",
                    )
                    if journal is not None and idempotency_key:
                        journal.upsert_end(
                            idempotency_key=idempotency_key,
                            state="detached", rc=_TIMEOUT_RC, ended_at=int(time.time()),
                        )
                    return _TIMEOUT_RC, out.getvalue(), (
                        err.getvalue() + "\ninfra_interruption: reconnect window exceeded"
                    ).lstrip()
                if not was_detached:
                    self._emit_proc_event(
                        "remote.proc.detached", node=node_id, proc_id=str(pid),
                    )
                    was_detached = True
                # Probe the node-agent to learn state + advance offsets. If the
                # probe itself fails we just sleep and retry on the next loop.
                try:
                    info = self._json(
                        "GET", f"/v1/sessions/{self._run_id}/procs/{pid}",
                    )
                except (RemoteUnavailableError, OSError, http.client.HTTPException):
                    pass
                else:
                    # Do NOT advance out_off/err_off to the server's file sizes:
                    # the client resumes from what it actually RECEIVED, or the
                    # output written during the outage is silently skipped.
                    # A finished proc is NOT a reason to return here: what it wrote
                    # during the outage is still on the node. Retry the stream from
                    # the offsets we actually received; it delivers the tail and a
                    # settled exit record. (The probe only tells us the node is back.)
                    if str(info.get("state", "")) in ("exited", "killed", "timeout",
                                                      "spawn_failed", "orphaned"):
                        backoff_s = _RECONNECT_BACKOFF_BASE_S
                        continue
                # Capped jittered exponential backoff; bounded by the window.
                sleep_s = min(backoff_s, max(0.05, reconnect_deadline - time.time()))
                sleep_s *= (1.0 + random.random()) / 2.0   # half-jitter
                time.sleep(max(0.05, sleep_s))
                backoff_s = min(backoff_s * 2.0, _RECONNECT_BACKOFF_CAP_S)
                continue

            # We got a fresh chunk from the stream. Reset backoff + emit reattach.
            if was_detached:
                self._emit_proc_event(
                    "remote.proc.reattached", node=node_id, proc_id=str(pid),
                    out_offset=out_off, err_offset=err_off,
                )
                was_detached = False
            backoff_s = _RECONNECT_BACKOFF_BASE_S

            got_data = False
            for line in chunk.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stream = msg.get("stream")
                data = msg.get("data", "")
                if stream == "out":
                    out.write(data)
                    out_off += len(data.encode("utf-8", "replace"))
                    if live_writer is not None and data:
                        live_writer.write_line(data, "stdout", partial=not data.endswith("\n"))
                    got_data = True
                elif stream == "err":
                    err.write(data)
                    err_off += len(data.encode("utf-8", "replace"))
                    if live_writer is not None and data:
                        live_writer.write_line(data, "stderr", partial=not data.endswith("\n"))
                    got_data = True
                elif stream == "exit":
                    last_state = str(msg.get("state", ""))
                    raw_rc = msg.get("rc")
                    # A killed proc can report its exit record before the
                    # node-agent's watcher has stored the OS code (rc=null).
                    if raw_rc is None:
                        last_rc = _KILLED_RC if last_state == "killed" else _SPAWN_FAILED_RC
                    else:
                        last_rc = int(raw_rc)
                    if last_state == "timeout":
                        last_rc = _TIMEOUT_RC
                    if last_state == "spawn_failed":
                        last_rc = _SPAWN_FAILED_RC
                    return last_rc, out.getvalue(), err.getvalue()

            # Checkpoint offsets every N seconds so a hard restart still has a
            # useful resume point for the next reconnect probe.
            if (journal is not None and idempotency_key
                    and (got_data or time.time() - last_offset_checkpoint >= _REMOTE_OFFSET_CHECKPOINT_S)):
                journal.upsert_offsets(idempotency_key, out_off, err_off)
                last_offset_checkpoint = time.time()

            if time.time() > timeout_deadline:
                # Client-side deadline (timeout + grace) — kill the proc and
                # report timeout. The node-agent also enforces timeout_s
                # server-side; the client grace is the additional buffer the
                # caller is willing to wait for the server's own kill to take
                # effect.
                self._kill_proc(pid)
                return _TIMEOUT_RC, out.getvalue(), (
                    err.getvalue() + f"\ntimeout after {timeout}s"
                ).lstrip()
            time.sleep(0.05)

    def _kill_proc(self, pid: int) -> None:
        """Best-effort POST /kill; never raises. Used by the reconnect-timeout path."""
        try:
            self._request(
                "POST",
                f"/v1/sessions/{self._run_id}/procs/{pid}/kill",
            )
        except RemoteUnavailableError:
            pass

    # ------------------------------------------------------------------ epic 09: live + kill

    def _resolve_pid_journal(self) -> Path | None:
        """Resolve the per-run pid journal path, or None when no run_dir.

        The journal lives under the run dir (``<run_dir>/.remote-pids.jsonl``)
        so the web control plane — which reads ``<home>/runs/<run_id>/...`` —
        can find the same pids without needing shared process memory. Lazy
        because a workspace constructed before ``MINI_ORK_RUN_DIR`` is set
        (e.g. an ad-hoc dispatch from the CLI) does not have a run_dir.
        """
        if self._pid_journal is not None:
            return self._pid_journal
        # The constructor's run_dir first: an out-of-process kill_run (the serve
        # process) has no MINI_ORK_RUN_DIR but knows the run dir it is killing.
        run_dir = (self._run_dir or os.environ.get("MINI_ORK_RUN_DIR") or "").strip()
        if not run_dir:
            return None
        self._pid_journal = Path(run_dir) / _PID_JOURNAL_NAME
        return self._pid_journal

    def _record_pid(self, pid: int, *, node_id: str, argv0: str) -> None:
        """Append ``{pid, node_id, argv0, ts}`` to the per-run journal.

        Best-effort: a missing run_dir (ad-hoc dispatch) means no journal,
        which means ``kill_all`` cannot target pids later — but ad-hoc
        dispatches also don't outlive the CLI invocation, so the lack of a
        cross-process kill path is not a regression. The lock guards against
        parallel-pool spawns racing on the file.
        """
        path = self._resolve_pid_journal()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._pid_journal_lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(
                        {
                            "pid": int(pid),
                            "node_id": node_id,
                            "argv0": argv0,
                            "ts": int(time.time() * 1000),
                        }
                    ) + "\n")
        except OSError:
            # Journal write is best-effort; the in-flight spawn is unaffected.
            pass

    def _emit_proc_event(self, event_type: str, **fields: Any) -> None:
        """Fire a ``remote.proc.start`` / ``remote.proc.exit`` event.

        Mirrors ``_emit_sync_event``: silent if the run-event emitter is
        unavailable (no DB in scope); the audit shape is what kickoff §5
        demands — ``{node, proc_id, node_host}`` for start,
        ``{rc, ms, killed}`` for exit. ``proc_id`` is the node-agent's pid
        stringified so a downstream consumer can correlate with the journal.
        """
        run_id = os.environ.get("MINI_ORK_RUN_ID", self._run_id)
        try:
            from mini_ork.observability.node_events import mo_node_emit
            mo_node_emit(
                run_id, node_id="remote-workspace", node_type="workspace",
                event_type=event_type, extra_json=json.dumps(fields),
            )
        except Exception:  # emitter must never break the dispatch
            pass

    def kill_all(self, *, budget_s: float = 10.0) -> bool:
        """Kill every spawned proc of this session within ``budget_s`` seconds.

        Reads the per-run pid journal and posts ``POST /kill`` per pid. The
        node-agent exposes no list-procs endpoint today, so the journal is
        the authoritative cross-process kill surface that the web control
        plane (``control.kill_run``) can reach without going through the
        dispatch process. Bounded: a missing journal, a transport error on
        the first kill, or a slow first kill does not blow the budget —
        every step is best-effort and the function never raises (the kill
        path's "best-effort" semantic mirrors ``Workspace.down()``).

        Returns True iff at least one pid was targeted. Returns False when
        no journal exists or no pids were recorded — both expected when
        the session has been idle (no spawns yet) or the run has no run_dir.
        """
        path = self._resolve_pid_journal()
        if path is None or not path.exists():
            return False
        deadline = time.monotonic() + max(0.0, budget_s)
        # Read once under the lock so a concurrent spawn's append does not
        # race the iteration; missing pids on the node-agent side are
        # absorbed as HTTPError → ignored (best-effort).
        with self._pid_journal_lock:
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                return False
        targeted = 0
        for raw_line in raw.splitlines():
            if time.monotonic() > deadline:
                break
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
                pid = int(entry.get("pid", 0))
            except (ValueError, json.JSONDecodeError):
                continue
            if pid <= 0:
                continue
            try:
                self._request(
                    "POST",
                    f"/v1/sessions/{self._run_id}/procs/{pid}/kill",
                )
                targeted += 1
            except RemoteUnavailableError:
                # Transport dead — nothing more we can do within budget.
                # The TTL reaper (``sandbox_reaper.reap_sandboxes``) is the
                # safety net for crashed runs that bypass teardown.
                continue
        return targeted > 0

    def put(self, content: str) -> str:
        if self._sid is None:
            raise RuntimeError("RemoteWorkspace.put called before up()")
        name = f"put-{uuid.uuid4().hex[:8]}.txt"
        # Tar the single file with a relative member name; the node-agent
        # extracts it under ``runs/<run_id>/run/<name>``.
        tar_bytes = self._tar_single_file(name, content.encode("utf-8"))
        self._request(
            "PUT",
            f"/v1/sessions/{self._run_id}/files",
            body=tar_bytes,
            content_type="application/x-tar",
            query={"root": _PUT_ROOT},
        )
        # Return the in-workspace path so the caller can pass it back to get();
        # same shape DockerWorkspace.put uses (an in-mount path).
        return f"{_PUT_ROOT}/{name}"

    def get(self, path: str) -> str:
        if self._sid is None:
            raise RuntimeError("RemoteWorkspace.get called before up()")
        # ``path`` is the in-workspace path returned by ``put()`` (e.g.
        # ``run/put-xxxxxxxx.txt``). The node-agent accepts the directory
        # part as the ``root`` and tar-packs its contents; we extract the
        # single member locally.
        root, _, member = path.partition("/")
        if not member:
            raise ValueError(f"get() requires a member path under a known root; got {path!r}")
        tar_bytes = self._request(
            "GET",
            f"/v1/sessions/{self._run_id}/files",
            query={"root": root},
        )
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
            for ti in tf:
                if ti.name == member and ti.isfile():
                    return tf.extractfile(ti).read().decode("utf-8")  # type: ignore[union-attr]
        raise FileNotFoundError(f"{path!r} not present in node-agent session")

    @staticmethod
    def _tar_single_file(name: str, content: bytes) -> bytes:
        """Tar ``name`` (relative, no slashes) with ``content`` as its body."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = tarfile.TarInfo(name=name)
            data.size = len(content)
            tf.addfile(data, io.BytesIO(content))
        return buf.getvalue()

    # ------------------------------------------------------------------ mirror

    def _upload_mo_home(self) -> None:
        """One-shot ``/workspace/mo-home`` upload (kickoff 08 req 2).

        Idempotent: a second call short-circuits at ``_mo_home_uploaded``.
        The deny-list is enforced by :mod:`mini_ork.remote.run_mirror` on
        the LOCAL path before the tar is built; the node-agent's tar
        guard then re-checks every member for path containment. ``MO_RECIPE``
        env names the recipe whose ``recipes/<name>/`` tree is included.
        """
        if self._mo_home_uploaded:
            return
        from mini_ork.remote import run_mirror  # lazy: keep top-level import-free
        home = os.environ.get("MINI_ORK_HOME", "").strip()
        if not home:
            self._mo_home_uploaded = True
            return
        recipe_name = os.environ.get("MO_RECIPE", "").strip() or None
        files = run_mirror.mo_home_files(home, recipe_name=recipe_name)
        if not files:
            self._mo_home_uploaded = True
            return
        tar_bytes = run_mirror.build_mo_home_tar(home, files)
        self._request(
            "PUT",
            f"/v1/sessions/{self._run_id}/files",
            body=tar_bytes,
            content_type="application/x-tar",
            query={"root": "mo-home"},
        )
        self._mo_home_uploaded = True
        self._emit_mirror_event(
            "remote.mirror.mo_home",
            files=len(files),
            bytes=len(tar_bytes),
        )
        return None

    def mirror_push(self) -> dict:
        """Push the local run dir to ``/workspace/run`` (kickoff 08 req 1+3).

        No-op when ``run_dir`` is unset (tests + the conformance suite).
        Otherwise: build the local manifest, ask the remote for its
        current manifest, diff, tar the changed set, and ``PUT`` the
        body. Per-file + per-sync size caps enforced before any read.
        Captures a snapshot for the upcoming ``mirror_pull`` conflict check.
        """
        if self._sid is None:
            raise RuntimeError("RemoteWorkspace.mirror_push called before up()")
        if not self._run_dir:
            return {"files": 0, "bytes": 0, "skipped": 0}
        with self._session_lock:
            from mini_ork.remote import run_mirror
            local_root = self._run_dir  # str; run_mirror.pathlib-converts internally
            local_manifest = run_mirror.manifest(
                local_root, excludes=run_mirror.DEFAULT_EXCLUDES
            )
            # The node-agent manifest endpoint requires a root query; pass
            # it explicitly so we don't depend on server-defaults semantics.
            remote_resp = self._json(
                "POST",
                f"/v1/sessions/{self._run_id}/files/manifest",
                {"root": _PUT_ROOT},
            )
            remote_manifest = (
                remote_resp.get("manifest", {}) if isinstance(remote_resp, dict) else {}
            )
            remote_sha = {rp: str(s) for rp, s in remote_manifest.items()}
            local_sha = {rp: t[0] for rp, t in local_manifest.items()}
            changed = run_mirror.diff_manifests(remote_sha, local_sha)
            max_file = _env_int_mb(
                "MO_REMOTE_MIRROR_MAX_FILE_MB", run_mirror.DEFAULT_MAX_FILE_MB
            )
            max_total = _env_int_mb(
                "MO_REMOTE_MIRROR_MAX_TOTAL_MB", run_mirror.DEFAULT_MAX_TOTAL_MB
            )
            skipped: list[tuple[str, int]] = []
            members_for_tar = {
                rp: local_manifest[rp] for rp in changed if rp in local_manifest
            }
            tar_bytes, stats = run_mirror.build_push_tar(
                local_root, members_for_tar,
                max_file_bytes=max_file, max_total_bytes=max_total,
                on_skip=lambda rp, sz: skipped.append((rp, sz)),
            )
            if tar_bytes:
                self._request(
                    "PUT",
                    f"/v1/sessions/{self._run_id}/files",
                    body=tar_bytes,
                    content_type="application/x-tar",
                    query={"root": _PUT_ROOT},
                )
            # Capture snapshot for pull-time conflict detection.
            self._mirror_push_snap = dict(local_sha)
            try:
                run_mirror.write_sidecar(local_root, local_sha)
            except OSError:
                pass  # sidecar is best-effort; resume still works in-memory
            self._emit_mirror_event(
                "remote.mirror.push",
                files=stats["files"],
                bytes=stats["bytes"],
                ms=stats["ms"],
            )
            for rp, sz in skipped:
                self._emit_mirror_event(
                    "remote.mirror.skipped",
                    file=rp,
                    size=sz,
                )
            return {
                "files": stats["files"],
                "bytes": stats["bytes"],
                "skipped": stats["skipped"],
            }

    def mirror_pull(self) -> dict:
        """Pull remote ``/workspace/run`` changes back to the local run dir.

        Detects conflicts by comparing the current local sha to the
        push-time snapshot: if they differ, the local copy wins (control
        plane never silently clobbers a process that ran during the
        spawn). Atomic temp+rename for non-conflicts; the freshly-written
        file's mtime is set to NOW so the executor's "agent wins by
        mtime" reader (``cli/execute_handlers.py:499-504``) keeps its
        meaning. An unchanged remote file is NOT rewritten (the reader
        falls back to stdout exactly as it does locally).
        """
        if self._sid is None:
            raise RuntimeError("RemoteWorkspace.mirror_pull called before up()")
        if not self._run_dir:
            return {"files": 0, "bytes": 0, "conflicts": 0}
        with self._session_lock:
            from mini_ork.remote import run_mirror
            local_root = self._run_dir
            local_at_push = dict(self._mirror_push_snap)
            if not local_at_push:
                # Resume path: read the on-disk sidecar so a control-plane
                # restart still has the push-time baseline.
                local_at_push = run_mirror.read_sidecar(local_root)
            local_now = run_mirror.manifest(
                local_root, excludes=run_mirror.DEFAULT_EXCLUDES
            )
            local_now_sha = {rp: t[0] for rp, t in local_now.items()}
            remote_resp = self._json(
                "POST",
                f"/v1/sessions/{self._run_id}/files/manifest",
                {"root": _PUT_ROOT},
            )
            remote_manifest = (
                remote_resp.get("manifest", {}) if isinstance(remote_resp, dict) else {}
            )
            remote_sha = {rp: str(s) for rp, s in remote_manifest.items()}
            changed = run_mirror.diff_manifests(local_at_push, remote_sha)
            if not changed:
                return {"files": 0, "bytes": 0, "conflicts": 0}
            start = time.time()
            tar_bytes = self._request(
                "GET",
                f"/v1/sessions/{self._run_id}/files",
                query={"root": _PUT_ROOT},
            )
            ms = int((time.time() - start) * 1000)
            conflicts, written, bytes_written = run_mirror.apply_pull_tar(
                local_root, tar_bytes, changed, local_at_push, local_now_sha,
                on_conflict=lambda rp: self._emit_mirror_event(
                    "remote.mirror.conflict",
                    file=rp,
                ),
            )
            self._emit_mirror_event(
                "remote.mirror.pull",
                files=written,
                bytes=bytes_written,
                ms=ms,
            )
            return {
                "files": written,
                "bytes": bytes_written,
                "conflicts": len(conflicts),
            }

    def _emit_mirror_event(self, event_type: str, **fields: Any) -> None:
        """Best-effort run_event emission; never raised.

        The mirror never breaks a run because observability is unhappy;
        :func:`mo_node_emit` itself is silent-no-op when ``state.db`` is
        missing (the unit-test path) or when an exception escapes the
        schema-aware insert. We still wrap in try/except because the
        emitter is a side-effecting import (``mo_node_emit`` lives in
        :mod:`mini_ork.observability.node_events`).
        """
        try:
            from mini_ork.observability.node_events import mo_node_emit
            mo_node_emit(
                run_id=self._run_id,
                node_id="remote-workspace",
                node_type="mirror",
                event_type=event_type,
                extra_json=json.dumps(fields, sort_keys=True),
            )
        except Exception:
            pass
        return None

    def down(self) -> None:
        # Cattle + idempotent: a missing session is a no-op (matches Docker
        # Workspace.down swallowing a missing container). The run dirs live on
        # the node until its reaper TTL — the dispatch layer owns the timer.
        if self._sid is None:
            return None
        if self._up_at is not None:
            self._emit_sync_event(
                "remote.run.summary", node_host=self._node.name or self._node.url,
                sync_up_bytes=self._sync_up_bytes, sync_down_bytes=self._sync_down_bytes,
                remote_wall_s=round(time.monotonic() - self._up_at, 3),
                setup_s=round(self._setup_s or 0.0, 3))
            self._up_at = None
        try:
            self._request("DELETE", f"/v1/sessions/{self._sid}")
        except RemoteUnavailableError:
            # ``down()`` is best-effort; the reaper TTLs the session. Do NOT
            # raise, or close_run_session will mask the original error.
            pass
        self._sid = None
        self._owns_session = False
        return None


def _factory(**kwargs: Any) -> RemoteWorkspace:
    """Build a :class:`RemoteWorkspace`, filling :env:`MO_NODE`-selected kwargs.

    Resolution order: explicit ``kwargs`` -> ``MO_NODE_URL``+``MO_NODE_TOKEN``
    one-off env -> :func:`mini_ork.remote.nodes.select_node` against
    ``$MO_NODE``. Missing node config raises a clear ``RuntimeError`` (never
    silently falls back to the host).

    The resolver passes its scoped ``env`` through the ``env=`` kwarg so a
    test (or a foreign-home run) does not have to mutate ``os.environ`` to
    steer node selection.
    """
    scoped_env = kwargs.pop("env", None)
    src = scoped_env if scoped_env is not None else os.environ
    # remote-nodes-14 §3: --env <profile> (MO_NODE_ENV) binds the session to
    # the profile's node, image (prepared on the node), resources and network.
    profile = kwargs.pop("profile", None)
    env_name = (src.get("MO_NODE_ENV") or "").strip()
    if profile is None and env_name:
        from mini_ork.remote.environments import load_profile

        profile = load_profile(env_name, env=src)
    node = kwargs.pop("node", None)
    if node is None:
        from mini_ork.remote.nodes import select_node

        node_src = src
        if profile is not None and getattr(profile, "node", None):
            node_src = {**src, "MO_NODE": profile.node}
        node = select_node(env=node_src)
    node_full = node
    if not isinstance(node, _NodeRef):
        # Accept a mini_ork.remote.nodes.Node by coercing its first 4 fields.
        node = _NodeRef(
            name=getattr(node, "name", ""),
            url=getattr(node, "url", ""),
            token=getattr(node, "token", ""),
            max_sessions=int(getattr(node, "max_sessions", 1) or 1),
        )
    if not node.url:
        raise RuntimeError(
            "remote workspace requires a node URL: pass node= or set "
            "MO_NODE_URL, or configure MO_NODE in config/nodes.yaml"
        )
    run_id = (
        kwargs.pop("run_id", None)
        or src.get("MINI_ORK_RUN_ID")
        or uuid.uuid4().hex[:12]
    )
    image = (kwargs.pop("image", None) or (getattr(profile, "image", None) if profile else None)
             or src.get("MO_SANDBOX_IMAGE"))
    if not image:
        raise RuntimeError(
            "remote workspace requires an image: pass image= or set "
            "MO_SANDBOX_IMAGE"
        )
    drive_root = kwargs.pop("drive_root", None) or src.get("MO_SHARED_DRIVE_ROOT")
    if not drive_root:
        drive_root = os.getcwd()
    if getattr(node_full, "token_env", ""):
        # A registry node's token lives in ITS token_env (e.g. MO_NODE_TOKEN_
        # STAGING_A), not the MO_NODE_TOKEN default the workspace would re-read.
        kwargs.setdefault("token_env", node_full.token_env)
    if src.get("MINI_ORK_RUN_DIR"):
        kwargs.setdefault("run_dir", src.get("MINI_ORK_RUN_DIR"))
    if src.get("MO_REMOTE_QUEUE_WAIT_S"):
        kwargs.setdefault("queue_wait_s", float(src["MO_REMOTE_QUEUE_WAIT_S"]))
    return RemoteWorkspace(
        node=node,
        run_id=run_id,
        image=image,
        drive_root=drive_root,
        profile=profile,
        **kwargs,
    )


def register() -> None:
    """Register the ``remote`` backend (idempotent, last-write-wins)."""
    register_workspace_backend("remote", _factory)


# Import-time effect: register the factory. This is the ONLY thing importing
# this module does — no socket, no env read, no subprocess — matching the
# contract ``docker.py`` keeps for itself. The resolver still imports this
# module lazily, so the default path (``MO_SANDBOX_BACKEND`` unset) never
# loads it; the verified-by-test for that is
# ``test_remote_module_not_imported_when_backend_unset``.
register()