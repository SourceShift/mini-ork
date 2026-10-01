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

import io
import json
import os
import random
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

    def _token(self) -> str:
        """Read the bearer token at call time (never at import)."""
        return os.environ.get(self._token_env, "")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        query: Mapping[str, str] | None = None,
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
        for attempt in range(self._retries):
            req = urllib_request.Request(url, data=body, method=method)
            if self._token():
                req.add_header("Authorization", f"Bearer {self._token()}")
            if content_type:
                req.add_header("Content-Type", content_type)
            try:
                with urllib_request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            except urllib_error.HTTPError as exc:
                # 5xx: transient, retry. 4xx: caller error (token, payload),
                # fail loud NOW.
                if 500 <= exc.code < 600 and attempt + 1 < self._retries:
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
                if attempt + 1 < self._retries:
                    time.sleep(_DEFAULT_BACKOFF_S * (1 + random.random()))
                    continue
                raise RemoteUnavailableError(
                    f"node-agent {method} {path} unreachable after "
                    f"{self._retries} attempts: {exc}"
                ) from exc
        # Loop fell off without raising — defensive.
        raise RemoteUnavailableError(
            f"node-agent {method} {path} failed: {last_exc}"
        )

    def _json(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        raw = self._request(method, path, body=body, content_type="application/json")
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

        # 1) Engine sha stage (idempotent).
        sha = self._current_engine_sha(subprocess)
        if sha not in self._uploaded_shas:
            health = self._json("GET", "/v1/health")
            remote_shas = {entry["sha"] for entry in health.get("engine_shas", [])}
            if sha not in remote_shas:
                bundle = self._bundle_engine(subprocess)
                self._request(
                    "PUT",
                    f"/v1/engines/{sha}",
                    body=bundle,
                    content_type="application/x-git-bundle",
                )
            self._uploaded_shas.add(sha)

        # 2) Session create (idempotent on run_id at the server).
        if self._sid is None:
            sess = self._json(
                "POST",
                "/v1/sessions",
                {"run_id": self._run_id, "image": self._image},
            )
            self._sid = sess.get("sid", self._run_id)
            self._owns_session = True
        # 3) Initial tree sync-up (epic 07) and the one-shot mo-home upload
        # (epic 08). Both are idempotent on a repeated up().
        with self._sync_lock:
            self._sync_up(force=True)
        self._upload_mo_home()
        return None

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
        """Produce a git bundle of the engine tree at HEAD; return its bytes."""
        with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            r = subprocess_mod.run(
                ["git", "-C", self._engine_root, "bundle", "create", tmp_path, "HEAD"],
                capture_output=True, text=True, check=False, timeout=300,
            )
            if r.returncode != 0:
                raise RemoteUnavailableError(
                    f"git bundle create failed for {self._engine_root!r}: "
                    f"{r.stderr.strip() or r.stdout.strip()}"
                )
            with open(tmp_path, "rb") as fh:
                return fh.read()
        finally:
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
        run_dir = (os.environ.get("MINI_ORK_RUN_DIR") or "").strip()
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
        if head_moved:
            self._emit_sync_event("remote.head_moved", tree=new_snap.tree)
        self._emit_sync_event("remote.sync.down", bytes=len(data), tree=new_snap.tree)

    def _emit_sync_event(self, event_type: str, **fields: Any) -> None:
        """Fire a ``remote.sync.*`` event through the run-event emitter.

        Silent if the emitter is unavailable (no DB in scope). The
        payload is what the kickoff §5 demands: ``bytes``,
        ``files_changed``, ``ms``, ``tree``. ``files_changed`` and
        ``ms`` are best-effort — we don't compute them here, callers
        may override via ``fields``.
        """
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

    def exec(self, cmd: str, *, cwd: str, timeout: int) -> tuple[int, str]:
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
        node_id = (env.get("MO_NODE_ID", "") if isinstance(env, Mapping) else "").strip()
        self._emit_proc_event("remote.proc.start", node=node_id, proc_id="pending",
                              node_host=self._node.name)
        start_ms = int(time.time() * 1000)
        rc = _SPAWN_FAILED_RC
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
                proc = self._json(
                    "POST",
                    f"/v1/sessions/{self._run_id}/procs",
                    {
                        "argv": argv_list,
                        "cwd": cwd or self._mount_path,
                        "env": dict(env),
                        "env_keys": sorted(env.keys()),
                        "stdin": stdin,
                        "timeout_s": timeout,
                    },
                )
                pid = int(proc["pid"])
                # The pid journal lets kill_run fan a kill out per pid (the
                # node-agent has no list-procs endpoint).
                self._record_pid(pid, node_id=node_id, argv0=argv_list[0] if argv_list else "")
                # 2) Drain the JSON-lines stream (out/err kept separate), teeing live.
                rc, out, err = self._drain_stream(pid, timeout, live_writer=live)
                # Post: the agent's tree edits come back as uncommitted changes.
                with self._sync_lock:
                    self._sync_down()
            finally:
                self.mirror_pull()   # the run dir comes back even on a sync conflict
            return rc, out, err
        finally:
            self._emit_proc_event("remote.proc.exit", node=node_id, rc=rc,
                                  ms=int(time.time() * 1000) - start_ms, killed=False)
            if live is not None:
                live.close()

    def _drain_stream(
        self,
        pid: int,
        timeout: float,
        *,
        live_writer: LiveWriter | None = None,
    ) -> tuple[int, str, str]:
        """Drain the proc stream endpoint until exit line; return (rc, out, err).

        Streams are kept separate (the spawn contract requires it; merging
        would corrupt the provider's JSON envelope on stdout). The node-agent
        also reports ``state="timeout"`` on its own timeout — we mirror that
        as the conventional ``rc=124`` with an empty stdout + stderr message.

        ``live_writer`` (remote-nodes-09): when provided, every out/err chunk
        is teed through ``LiveWriter.write_line`` so a tailer polling the
        live sidecar sees each line as it arrives, not at exit.
        """
        out = io.StringIO()
        err = io.StringIO()
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
        deadline = time.time() + timeout + grace_s
        last_rc: int = 0
        last_state: str = ""
        while True:
            try:
                chunk = self._request(
                    "GET",
                    f"/v1/sessions/{self._run_id}/procs/{pid}/stream",
                )
            except RemoteUnavailableError:
                # Transport died mid-drain — treat as a timeout (we cannot
                # tell the difference from the caller's side; either way the
                # caller should retry from scratch).
                if time.time() > deadline:
                    return _TIMEOUT_RC, out.getvalue(), (
                        err.getvalue() + f"\ntimeout after {timeout}s"
                    ).lstrip()
                time.sleep(0.05)
                continue
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
                    if live_writer is not None and data:
                        live_writer.write_line(data, "stdout", partial=not data.endswith("\n"))
                elif stream == "err":
                    err.write(data)
                    if live_writer is not None and data:
                        live_writer.write_line(data, "stderr", partial=not data.endswith("\n"))
                elif stream == "exit":
                    last_rc = int(msg.get("rc", _SPAWN_FAILED_RC))
                    last_state = str(msg.get("state", ""))
                    if last_state == "timeout":
                        last_rc = _TIMEOUT_RC
                    if last_state == "spawn_failed":
                        last_rc = _SPAWN_FAILED_RC
                    return last_rc, out.getvalue(), err.getvalue()
            if time.time() > deadline:
                # Client-side deadline (timeout + grace) — kill the proc and
                # report timeout. The node-agent also enforces timeout_s
                # server-side; the client grace is the additional buffer the
                # caller is willing to wait for the server's own kill to take
                # effect.
                try:
                    self._request(
                        "POST",
                        f"/v1/sessions/{self._run_id}/procs/{pid}/kill",
                    )
                except RemoteUnavailableError:
                    pass
                return _TIMEOUT_RC, out.getvalue(), (
                    err.getvalue() + f"\ntimeout after {timeout}s"
                ).lstrip()
            time.sleep(0.05)

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
    node = kwargs.pop("node", None)
    if node is None:
        from mini_ork.remote.nodes import select_node

        node = select_node(env=src)
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
    image = kwargs.pop("image", None) or src.get("MO_SANDBOX_IMAGE")
    if not image:
        raise RuntimeError(
            "remote workspace requires an image: pass image= or set "
            "MO_SANDBOX_IMAGE"
        )
    drive_root = kwargs.pop("drive_root", None) or src.get("MO_SHARED_DRIVE_ROOT")
    if not drive_root:
        drive_root = os.getcwd()
    return RemoteWorkspace(
        node=node,
        run_id=run_id,
        image=image,
        drive_root=drive_root,
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