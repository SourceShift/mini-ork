"""Per-node human-review decision layer over ``mo_inbox_gates``.

The executor (a separate, later wiring change) will call ``decide`` BEFORE
dispatching each node. ``decide`` inspects prior successful checkpoints that
match the policy and returns one of:

* ``("proceed", {})`` — nothing is waiting; dispatch may proceed.
* ``("park", {"awaiting": [...], "inbox_ids": [...]})`` — at least one review
  is pending; dispatch must wait.
* ``("block", {...})`` — a reviewer rejected, the loader raised, or the
  node-checkpoint lookup failed in a way that should never be auto-rescued.

The fast path is "policy has no ``after_nodes``": ``decide`` returns
``("proceed", {})`` WITHOUT touching the DB. A loader ``ValueError`` is
fail-closed (block) regardless of ``after_nodes`` so an operator typo in
``MO_REVIEW_BEFORE_MERGE`` cannot silently drop the gate.

Resolution verbs (``approve_node``, ``request_changes``) refuse inside a
run: an agent that wrote the diff must never be the one clicking Approve.
The check mirrors ``mini_ork.gates.merge_approval._refuse_if_in_run`` because
the two paths must agree on what "inside a run" means.

``REVIEW_PAUSE_RC = 9`` is the exit code a future executor wiring change
will use to terminate a parked run so the operator can resume with
``mini-ork recover <run_id> --strategy resume``. Declaring it here means no
rename when the wiring lands.

Gate row shape (one row per ``node_checkpoints`` success):

    gate_id    = f"node:{run_id}:{node_id}:{created_at}"
    feature    = "review.after_node"
    phase      = node_type
    context    = {run_id, node_id, node_type, created_at, ckpt_ref}
    blocks_dispatch_for = run_id

Including ``created_at`` in ``gate_id`` means a re-run of the same node gets
a fresh gate and the old approval no longer covers it (DoD 5).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from typing import Any, Mapping

from mini_ork.gates import oversight_inbox
from mini_ork.gates.merge_approval import ApprovalError
from mini_ork.gates.review_policy import (
    ReviewPolicy,
    load_policy,
    node_requires_review,
)

__all__ = [
    "REVIEW_PAUSE_RC",
    "FEATURE",
    "reviewable_completed",
    "decide",
    "pending_for_run",
    "approve_node",
    "request_changes",
]


REVIEW_PAUSE_RC = 9
FEATURE = "review.after_node"

# Env keys that mark "this is happening inside a running agent". An agent
# must never approve its own run.
_IN_RUN_KEYS = ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR")


def _db(db_path: str | os.PathLike[str] | None) -> str:
    if db_path is not None:
        return os.fspath(db_path)
    return os.environ.get("MINI_ORK_DB") or ".mini-ork/state.db"


def _connect(db_path: str | os.PathLike[str] | None) -> sqlite3.Connection:
    """Open the SQLite handle and ensure ``mo_inbox_gates`` is bootstrapped.

    Reuses ``oversight_inbox._connect`` so every reader of the inbox sees the
    same schema. ``node_checkpoints`` / ``node_attempts`` are production
    tables created by migration ``0050``; this function never creates them —
    callers and tests must arrange DDL up front (a fresh tmp DB will raise
    ``OperationalError`` on a query, which is the intended fail-closed path).
    """
    conn = oversight_inbox._connect(_db(db_path))  # noqa: SLF001 — module-private, intent is to share bootstrap
    conn.row_factory = sqlite3.Row
    return conn


def _gate_id(run_id: str, node_id: str, created_at: int) -> str:
    return f"node:{run_id}:{node_id}:{int(created_at)}"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    raw = out.pop("context_json", None)
    try:
        out["context"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        out["context"] = {}
    return out


def _node_type(run_id: str, node_id: str, db_path: str | os.PathLike[str] | None,
               run_dir: str | os.PathLike[str] | None) -> str:
    """Latest ``node_attempts.node_type`` for ``(run_id, node_id)``.

    Falls back to ``<run_dir>/plan.json`` ``decomposition[*].node_type`` when
    the attempts table has no non-empty entry (e.g. a retry row was written
    without a type, or the run predates the attempt log). An empty string
    when both sources are bare — the caller treats that as "type unknown"
    and matches it via the fallback ``node_id in policy.after_nodes`` arm.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT node_type FROM node_attempts "
            "WHERE run_id = ? AND node_id = ? "
            "AND node_type IS NOT NULL AND node_type != '' "
            "ORDER BY attempt_no DESC, id DESC LIMIT 1",
            (run_id, node_id),
        ).fetchone()
    finally:
        conn.close()
    if row is not None and row["node_type"]:
        return row["node_type"]

    if not run_dir:
        return ""
    plan_path = os.path.join(os.fspath(run_dir), "plan.json")
    if not os.path.isfile(plan_path):
        return ""
    try:
        with open(plan_path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(plan, dict):
        return ""
    for step in plan.get("decomposition") or []:
        if isinstance(step, dict) and step.get("id") == node_id:
            return str(step.get("node_type") or "")
    return ""


def _ckpt_ref(run_id: str, node_id: str,
              run_dir: str | os.PathLike[str] | None,
              *, env: Mapping[str, str] | None = None) -> str:
    """Resolve ``refs/mini-ork/ckpt/<run_id>/<node_id>`` against the run tree.

    Best-effort and never raises. Returns the ref string when
    ``git show-ref --verify --quiet`` succeeds against ``load_run_roots(
    run_dir).target`` (else ``$MO_TARGET_CWD``), otherwise ``""``. An empty
    ``run_dir`` AND empty ``MO_TARGET_CWD`` skip git entirely — running from
    the engine repo would otherwise probe the wrong tree.
    """
    target = ""
    if run_dir:
        try:
            from mini_ork.runtime.run_roots import load_run_roots
            roots = load_run_roots(os.fspath(run_dir))
        except Exception:  # noqa: BLE001 — best-effort, must not raise
            roots = None
        if roots is not None and getattr(roots, "target", ""):
            target = roots.target
    if not target:
        effective_env = os.environ if env is None else env
        target = (effective_env.get("MO_TARGET_CWD") or "") if effective_env else ""
    if not target:
        return ""

    ref = f"refs/mini-ork/ckpt/{run_id}/{node_id}"
    try:
        proc = subprocess.run(
            ["git", "-C", target, "show-ref", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return ref if proc.returncode == 0 else ""


def reviewable_completed(
    run_id: str,
    *,
    db_path: str | os.PathLike[str] | None,
    run_dir: str | os.PathLike[str] | None,
    policy: ReviewPolicy,
) -> list[dict[str, Any]]:
    """Every ``node_checkpoints`` success row whose node matches the policy.

    A node matches when either ``node_id in policy.after_nodes`` OR the
    helper ``node_requires_review(policy, node_type)`` says yes (the helper
    covers the ``("*",)`` star case and the type-vs-id ladder). Items are
    ordered by ``created_at`` so the dispatch order is reproducible.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT node_id, created_at FROM node_checkpoints "
            "WHERE run_id = ? AND status = 'success'",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        node_id = r["node_id"]
        node_type = _node_type(run_id, node_id, db_path, run_dir)
        if node_id in policy.after_nodes or node_requires_review(policy, node_type):
            out.append(
                {
                    "node_id": node_id,
                    "node_type": node_type,
                    "created_at": int(r["created_at"]),
                }
            )
    out.sort(key=lambda item: (item["created_at"], item["node_id"]))
    return out


def _latest_for_gate(
    run_id: str,
    node_id: str,
    created_at: int,
    db_path: str | os.PathLike[str] | None,
) -> sqlite3.Row | None:
    """The most recent inbox row whose ``gate_id`` matches this checkpoint."""
    gate = _gate_id(run_id, node_id, created_at)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mo_inbox_gates WHERE gate_id = ? "
            "ORDER BY inbox_id DESC LIMIT 1",
            (gate,),
        ).fetchone()
    finally:
        conn.close()
    return row


def _resolve_or_enqueue(
    run_id: str,
    node: Mapping[str, Any],
    gate: str,
    *,
    db_path: str | os.PathLike[str] | None,
    run_dir: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
) -> tuple[int | None, str | None, str]:
    """Atomically resolve or enqueue the inbox row for ``gate``.

    The check-then-insert must be one transaction: three verifiers dispatched
    in parallel after a gated node all see "no row" without a lock, each
    enqueues one, and ``approve_node`` only resolves the newest — leaving
    duplicates pending forever. ``BEGIN IMMEDIATE`` acquires the write lock up
    front so concurrent transactions serialise.

    Returns ``(inbox_id, status, review_note)``:

    * ``status="approved"`` → row exists, no work to do.
    * ``status="rejected"`` → row exists, the dispatch must block.
    * ``status="pending"``  → row was just enqueued (or pre-existed pending).
    """
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT inbox_id, status, review_note FROM mo_inbox_gates "
                "WHERE gate_id = ? ORDER BY inbox_id DESC LIMIT 1",
                (gate,),
            ).fetchone()
            if row is not None:
                return int(row["inbox_id"]), str(row["status"]), str(row["review_note"] or "")
            ctx = {
                "run_id": run_id,
                "node_id": node["node_id"],
                "node_type": node["node_type"],
                "created_at": int(node["created_at"]),
                "ckpt_ref": _ckpt_ref(run_id, node["node_id"], run_dir, env=env),
            }
            cur = conn.execute(
                "INSERT INTO mo_inbox_gates "
                "(gate_id, feature, phase, context_json, status, enqueued_at, "
                " blocks_dispatch_for) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (
                    gate,
                    FEATURE,
                    node["node_type"],
                    json.dumps(ctx),
                    time.time(),
                    run_id,
                ),
            )
            new_id = int(cur.lastrowid or 0)
            conn.commit()
            return new_id, "pending", ""
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _latest_pending_node_inbox(
    run_id: str,
    node_id: str,
    db_path: str | os.PathLike[str] | None,
) -> int | None:
    """The latest ``status='pending'`` inbox row for the run+node pair.

    Filters in Python rather than via ``LIKE``: node ids contain ``_``
    (e.g. ``static_check_verifier``), and ``LIKE`` treats ``_`` as a
    single-character wildcard, so two sibling nodes (``foo`` vs ``foobar``)
    would collide. The exact ``gate_id`` would be unambiguous, but a later
    inbox-row migration could add siblings that didn't exist when this
    lookup was written.
    """
    prefix = f"node:{run_id}:{node_id}:"
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT inbox_id, gate_id, context_json FROM mo_inbox_gates "
            "WHERE status = 'pending' "
            "ORDER BY inbox_id DESC",
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        gate = r["gate_id"] or ""
        if not gate.startswith(prefix):
            continue
        try:
            ctx = json.loads(r["context_json"]) if r["context_json"] else {}
        except (TypeError, ValueError):
            ctx = {}
        if not isinstance(ctx, dict):
            continue
        if ctx.get("run_id") == run_id and ctx.get("node_id") == node_id:
            return int(r["inbox_id"])
    return None


def _refuse_if_in_run(env: Mapping[str, str]) -> None:
    """An agent must never approve its own run."""
    for key in _IN_RUN_KEYS:
        if env.get(key):
            raise ApprovalError(
                f"refuses inside a run: {key} is set in the environment; "
                "a human must approve, not the agent that produced the diff"
            )


def decide(
    run_id: str,
    node_id: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
    run_dir: str | os.PathLike[str] | None = None,
    policy: ReviewPolicy | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """``(decision, payload)`` — ``"proceed" | "park" | "block"``.

    * ``policy`` is honoured when passed; otherwise ``load_policy(env=env)``.
    * A loader ``ValueError`` is fail-closed → ``("block", {"error": str(e)})``.
    * ``after_nodes`` empty → ``("proceed", {})`` **without** opening the DB.
    * Every other exception once ``after_nodes`` is non-empty is also block.
    """
    try:
        effective_policy = policy if policy is not None else load_policy(env=env)
    except ValueError as exc:
        return ("block", {"error": str(exc)})

    # Fast path: nothing is gated, nothing to read. Must run before ANY DB
    # resolution because the kickoff's DoD 1 asserts the DB file is never
    # created on this branch.
    if not effective_policy.after_nodes:
        return ("proceed", {})

    try:
        return _decide_with_db(
            run_id,
            node_id,
            db_path=db_path,
            run_dir=run_dir,
            policy=effective_policy,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed over every error
        return ("block", {"error": str(exc)})


def _decide_with_db(
    run_id: str,
    node_id: str,
    *,
    db_path: str | os.PathLike[str] | None,
    run_dir: str | os.PathLike[str] | None,
    policy: ReviewPolicy,
    env: Mapping[str, str] | None,
) -> tuple[str, dict[str, Any]]:
    """The DB-using half of ``decide`` — wrapped in a single try/except at
    the call site so a single point of failure surfaces as ``block``."""
    nodes = reviewable_completed(
        run_id,
        db_path=db_path,
        run_dir=run_dir,
        policy=policy,
    )
    awaiting: list[str] = []
    inbox_ids: list[int] = []
    for node in nodes:
        if node["node_id"] == node_id:
            # The dispatch is for this very node; reviewing one's own
            # in-flight output is meaningless. Skip it.
            continue
        gate = _gate_id(run_id, node["node_id"], node["created_at"])
        inbox_id, status, review_note = _resolve_or_enqueue(
            run_id, node, gate, db_path=db_path, run_dir=run_dir, env=env
        )
        if inbox_id is None and status is None:
            # Should not happen: _resolve_or_enqueue returns a status OR None.
            continue
        if status == "approved":
            continue
        if status == "rejected":
            return (
                "block",
                {
                    "rejected": dict(node),
                    "note": review_note or "",
                    "inbox_id": int(inbox_id) if inbox_id is not None else 0,
                },
            )
        # status == "pending" — awaiting review.
        awaiting.append(node["node_id"])
        inbox_ids.append(int(inbox_id) if inbox_id is not None else 0)

    if awaiting:
        return ("park", {"awaiting": awaiting, "inbox_ids": inbox_ids})
    return ("proceed", {})


def pending_for_run(
    run_id: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Pending ``node:{run_id}:*`` inbox rows for the run."""
    prefix = f"node:{run_id}:"
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM mo_inbox_gates "
            "WHERE feature = ? AND status = 'pending' "
            "ORDER BY inbox_id ASC",
            (FEATURE,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        gate = r["gate_id"] or ""
        if not gate.startswith(prefix):
            continue
        decoded = _row_to_dict(r)
        if decoded is not None:
            out.append(decoded)
    return out


def approve_node(
    run_id: str,
    node_id: str,
    *,
    approver: str,
    note: str = "",
    db_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve the latest pending node review for ``(run_id, node_id)`` as
    ``approved``. Returns the ``inbox_id``."""
    effective_env = os.environ if env is None else env
    _refuse_if_in_run(effective_env)

    inbox_id = _latest_pending_node_inbox(run_id, node_id, db_path)
    if inbox_id is None:
        raise ApprovalError(
            f"no pending node review for {node_id!r} in run {run_id!r}"
        )
    review_note = f"{approver}: {note}"
    ok = oversight_inbox.resolve(
        inbox_id,
        "approved",
        review_note=review_note,
        db_path=db_path,
    )
    if not ok:
        raise ApprovalError(
            f"no pending node review resolved for {node_id!r} in run {run_id!r}; "
            "another reviewer may have already resolved it"
        )
    return inbox_id


def request_changes(
    run_id: str,
    node_id: str,
    *,
    approver: str,
    note: str,
    db_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve the latest pending node review for ``(run_id, node_id)`` as
    ``rejected``. Returns the ``inbox_id``."""
    effective_env = os.environ if env is None else env
    _refuse_if_in_run(effective_env)

    inbox_id = _latest_pending_node_inbox(run_id, node_id, db_path)
    if inbox_id is None:
        raise ApprovalError(
            f"no pending node review for {node_id!r} in run {run_id!r}"
        )
    review_note = f"{approver}: {note}"
    ok = oversight_inbox.resolve(
        inbox_id,
        "rejected",
        review_note=review_note,
        db_path=db_path,
    )
    if not ok:
        raise ApprovalError(
            f"no pending node review resolved for {node_id!r} in run {run_id!r}; "
            "another reviewer may have already resolved it"
        )
    return inbox_id
