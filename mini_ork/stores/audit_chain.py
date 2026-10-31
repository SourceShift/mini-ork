"""Hash-chained, append-only audit trail over the existing ``audit_log`` table.

``audit_log`` (db/migrations/0012_safety.sql) has been append-only by trigger
since it was created, but nothing in the Python runtime wrote to it, so the
record of what the loop dispatched, verified and promoted lived only in tables
the runtime can rewrite. This module is its writer.

Every row carries its own hash and its predecessor's, inside the JSON
``payload`` (the table's columns are frozen by docs/SAFETY.md):

    payload = {"chain": "audit-chain/v1", "event": {...},
               "prev_hash": <hex>, "row_hash": <hex>}
    row_hash = sha256(canonical({prev_hash, event_type, actor, target,
                                 occurred_at, event}))

The triggers stop an UPDATE or DELETE through SQLite; the chain makes an edit
made around them (a rewritten file, a dropped trigger) detectable by anyone
holding the database, without trusting the runtime that wrote it:
``verify_chain`` recomputes every link.

A writer appends inside the caller's transaction, AFTER the caller's own write,
so the read of the previous hash happens under the write lock that statement
took and two writers cannot fork the chain.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

CHAIN_VERSION = "audit-chain/v1"
GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def row_hash(prev_hash: str, event_type: str, actor: str, target: str,
             occurred_at: str, event: Mapping[str, Any]) -> str:
    """The hash that seals one row and links it to ``prev_hash``."""
    body = _canonical({
        "prev_hash": prev_hash,
        "event_type": event_type,
        "actor": actor,
        "target": target,
        "occurred_at": occurred_at,
        "event": dict(event),
    })
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _has_table(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchone() is not None


def _chained(payload: str) -> dict | None:
    try:
        data = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict) and data.get("chain") == CHAIN_VERSION:
        return data
    return None


def _last_hash(con: sqlite3.Connection) -> str:
    for (payload,) in con.execute("SELECT payload FROM audit_log ORDER BY audit_id DESC"):
        data = _chained(payload)
        if data is not None:
            return str(data.get("row_hash") or GENESIS)
    return GENESIS


def append_event(con: sqlite3.Connection, event_type: str, actor: str, target: str,
                 event: Mapping[str, Any]) -> dict | None:
    """Append one chained row inside the caller's open transaction.

    Returns ``{"audit_id", "row_hash"}``, or ``None`` when the database has no
    ``audit_log`` table (an ad-hoc, unmigrated database). The caller commits.
    """
    if not _has_table(con):
        return None
    prev = _last_hash(con)
    occurred_at = _now_iso()
    sealed = row_hash(prev, event_type, actor, target, occurred_at, event)
    payload = _canonical({
        "chain": CHAIN_VERSION,
        "event": dict(event),
        "prev_hash": prev,
        "row_hash": sealed,
    })
    cur = con.execute(
        "INSERT INTO audit_log (event_type, actor, target, payload, occurred_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type, actor, target, payload, occurred_at),
    )
    return {"audit_id": cur.lastrowid, "row_hash": sealed}


def append_event_soft(con: sqlite3.Connection, event_type: str, actor: str, target: str,
                      event: Mapping[str, Any]) -> dict | None:
    """``append_event`` that never fails the caller's own write.

    A locked or damaged audit table must not turn a successful dispatch or
    promotion into an error; the miss is reported on stderr instead.
    """
    try:
        return append_event(con, event_type, actor, target, event)
    except sqlite3.Error as exc:
        sys.stderr.write(f"[audit] row not written ({event_type} {target}): {exc}\n")
        return None


def record(db_path: str, event_type: str, actor: str, target: str,
           event: Mapping[str, Any]) -> dict | None:
    """Append one row on its own connection and transaction (no caller write)."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("BEGIN IMMEDIATE")
        sealed = append_event(con, event_type, actor, target, event)
        con.commit()
        return sealed
    except sqlite3.Error:
        con.rollback()
        raise
    finally:
        con.close()


def verify_chain(db_path: str) -> dict:
    """Recompute every link of the chain in ``db_path``.

    ``ok`` is False at the first row whose stored hash does not match its
    content, whose ``prev_hash`` does not name the row before it, or that sits
    unchained after the chain began (a row inserted around the writer). Rows
    written before the chain existed form an unchained prefix and are counted,
    not failed.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not _has_table(con):
            return {"ok": False, "rows": 0, "chained_rows": 0, "head": None,
                    "first_break": None, "reason": "no audit_log table"}
        prev = GENESIS
        rows = chained = 0
        started = False
        for audit_id, event_type, actor, target, payload, occurred_at in con.execute(
            "SELECT audit_id, event_type, actor, target, payload, occurred_at "
            "FROM audit_log ORDER BY audit_id"
        ):
            rows += 1
            data = _chained(payload)
            if data is None:
                if started:
                    return {"ok": False, "rows": rows, "chained_rows": chained, "head": prev,
                            "first_break": audit_id, "reason": "unchained row after the chain began"}
                continue
            started = True
            if data.get("prev_hash") != prev:
                return {"ok": False, "rows": rows, "chained_rows": chained, "head": prev,
                        "first_break": audit_id, "reason": "prev_hash does not link to the row before"}
            expected = row_hash(prev, event_type, actor, target, occurred_at, data.get("event") or {})
            if data.get("row_hash") != expected:
                return {"ok": False, "rows": rows, "chained_rows": chained, "head": prev,
                        "first_break": audit_id, "reason": "row content does not match its hash"}
            prev = expected
            chained += 1
        return {"ok": True, "rows": rows, "chained_rows": chained,
                "head": prev if chained else None, "first_break": None, "reason": ""}
    finally:
        con.close()


def find_row(db_path: str, audit_id: int) -> dict | None:
    """One audit row with its decoded payload, or ``None``."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT audit_id, event_type, actor, target, payload, occurred_at "
            "FROM audit_log WHERE audit_id=?", (audit_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    data = _chained(row[4]) or {}
    return {"audit_id": row[0], "event_type": row[1], "actor": row[2], "target": row[3],
            "occurred_at": row[5], "event": data.get("event"), "row_hash": data.get("row_hash")}


__all__ = [
    "CHAIN_VERSION", "GENESIS", "append_event", "append_event_soft", "find_row",
    "record", "row_hash", "verify_chain",
]
