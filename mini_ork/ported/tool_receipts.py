"""Tool-call receipts for safe replay during recovery (durable-dag E4).

Design source: ``internal-docs/architecture/2026-07-15-durable-dag-resume-design.md`` §6.
Schema: ``db/migrations/0053_tool_receipts.sql``.

When a recovered node replays work, a side-effecting (non-idempotent) tool
call it already made — a git commit, a file write, an external POST — must NOT
run a second time. This store records a *receipt* (the canonical input hash +
the captured output) the first time such a call completes; on replay the guard
``replay_or_invoke`` returns the receipt WITHOUT re-invoking (scenario 8).
Read-only tools are marked ``idempotent`` and re-invoke fresh by default.

Scope note (honest): the claude lane runs its OWN internal tool loop inside the
``claude --print`` subprocess, so mini-ork cannot intercept claude's per-tool
Read/Write/Bash calls to replay them. This store is for the side effects
mini-ork itself performs at node boundaries (the publisher commit, checkpoint
writes, external calls a recipe drives) and is the durable substrate a future
in-loop interceptor would write through. The design's "read-only may replay /
non-idempotent must not" rule is enforced here for those callers.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from typing import Any, Callable, Optional

__all__ = [
    "input_hash",
    "record_receipt",
    "get_receipt",
    "has_receipt",
    "replay_or_invoke",
]

_BUSY_MS = 5000


def _log(msg: str) -> None:
    sys.stderr.write(f"tool_receipts: {msg}\n")


def _open(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=_BUSY_MS / 1000)
    con.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
    return con


def input_hash(tool_input: Any) -> str:
    """Canonical sha256 of a tool input. Dicts/lists are dumped with sorted
    keys so the hash is stable across equal-but-reordered inputs; anything else
    is stringified. This is the idempotency key — the same call maps to the
    same receipt."""
    if isinstance(tool_input, (dict, list)):
        blob = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    else:
        blob = str(tool_input)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _receipt_id(run_id: str, node_id: str, tool_name: str, ih: str) -> str:
    return hashlib.sha256(f"{run_id}|{node_id}|{tool_name}|{ih}".encode()).hexdigest()[:32]


def record_receipt(
    db: str,
    run_id: str,
    node_id: str,
    tool_name: str,
    tool_input: Any,
    output: Any,
    *,
    idempotent: bool = False,
    status: str = "completed",
    attempt: int = 1,
    now: Optional[int] = None,
) -> str:
    """Persist a receipt for one tool call. Returns the receipt_id, or "" on a
    hard DB error. UPSERT on (run_id, node_id, tool_name, input_hash)."""
    if not db or not run_id or not node_id or not tool_name:
        _log("record_receipt: run_id, node_id, tool_name required")
        return ""
    if status not in ("completed", "failed"):
        _log(f"record_receipt: bad status {status!r}")
        return ""
    ih = input_hash(tool_input)
    rid = _receipt_id(run_id, node_id, tool_name, ih)
    ts = int(now) if now is not None else int(time.time())
    try:
        payload = json.dumps(output, default=str)
    except (TypeError, ValueError):
        payload = json.dumps(str(output))
    try:
        con = _open(db)
        try:
            con.execute(
                "INSERT INTO tool_receipts"
                "(receipt_id, run_id, node_id, attempt, tool_name, input_hash, "
                " idempotent, output_json, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id, node_id, tool_name, input_hash) DO UPDATE SET "
                "  output_json=excluded.output_json, status=excluded.status, "
                "  attempt=excluded.attempt, idempotent=excluded.idempotent, "
                "  created_at=excluded.created_at",
                (rid, run_id, node_id, int(attempt), tool_name, ih,
                 1 if idempotent else 0, payload, status, ts),
            )
            con.commit()
            return rid
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"record_receipt: {e}")
        return ""


def get_receipt(
    db: str, run_id: str, node_id: str, tool_name: str, tool_input: Any
) -> Optional[dict]:
    """Look up a receipt by its idempotency key. Returns a dict with keys
    ``output`` (decoded), ``idempotent`` (bool), ``status`` (str), or None."""
    if not db or not run_id or not node_id or not tool_name:
        return None
    ih = input_hash(tool_input)
    try:
        con = _open(db)
        try:
            row = con.execute(
                "SELECT output_json, idempotent, status FROM tool_receipts "
                "WHERE run_id=? AND node_id=? AND tool_name=? AND input_hash=?",
                (run_id, node_id, tool_name, ih),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"get_receipt: {e}")
        return None
    if row is None:
        return None
    try:
        out = json.loads(row[0]) if row[0] is not None else None
    except (TypeError, ValueError):
        out = row[0]
    return {"output": out, "idempotent": bool(row[1]), "status": row[2]}


def has_receipt(db: str, run_id: str, node_id: str, tool_name: str, tool_input: Any) -> bool:
    return get_receipt(db, run_id, node_id, tool_name, tool_input) is not None


def replay_or_invoke(
    db: str,
    run_id: str,
    node_id: str,
    tool_name: str,
    tool_input: Any,
    invoke: Callable[[], Any],
    *,
    idempotent: bool = False,
    attempt: int = 1,
) -> Any:
    """The replay guard (scenario 8).

    * A COMPLETED, non-idempotent receipt exists → return its output WITHOUT
      calling ``invoke`` (a side-effecting tool never fires twice).
    * A completed idempotent (read-only) receipt → re-invoke fresh (read-only
      results are cheap and may have changed), then re-record.
    * No receipt (or a prior ``failed`` receipt) → call ``invoke``, record the
      result as a completed receipt, return it.

    ``invoke`` is a zero-arg thunk that actually performs the tool call.
    """
    existing = get_receipt(db, run_id, node_id, tool_name, tool_input)
    if existing is not None and existing["status"] == "completed" and not existing["idempotent"]:
        return existing["output"]  # ← side effect already happened; DO NOT re-invoke
    # read-only receipt or no/failed receipt → run it
    result = invoke()
    record_receipt(
        db, run_id, node_id, tool_name, tool_input, result,
        idempotent=idempotent, status="completed", attempt=attempt,
    )
    return result
