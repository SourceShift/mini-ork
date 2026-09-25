"""Enqueue/resolve API over the ``mo_inbox_gates`` human-oversight channel.

``mo_inbox_gates`` has existed in the schema since migration 0002 — described
in its own comment as "human-approval gates inserted into the mini-orch flow at
key phase boundaries" — and had no writer and no reader anywhere in ``mini_ork``.
A wired-and-dead channel fails invisibly, in one of two directions: **rule by
fatigue** (it fires so often that approving stops being a judgment) or
**abandoned oversight** (it enqueues and nothing is ever resolved, so the gate
is off while everyone believes it is on). Neither is visible until something
counts.

This module is the write half of making the channel measurable. It is
deliberately passive: nothing in the executor calls it, no gate is registered,
and ``gates/intervention_gate.py`` keeps its fail-open contract exactly. Wiring
the executor to enqueue is a separate, higher-stakes policy change.

Self-bootstrap mirrors ``mini_ork.memory.semantic._connect``: ``_connect()``
runs an idempotent ``CREATE TABLE IF NOT EXISTS`` copy of the canonical DDL on
every open, so the module works against a fresh tmp DB without the migration
runner ever running.

Resolution is one-way, matching the one-way ledger discipline in
``semantic.record_outcome``: the UPDATE only matches ``status='pending'``, so a
second resolve of the same row changes nothing and returns ``False``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

__all__ = ["enqueue", "resolve", "pending", "get"]

# Idempotent copy of the canonical DDL (db/migrations/0002_mini_orch_sessions.sql:
# mo_inbox_gates + its partial pending index). Re-running is a no-op.
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS mo_inbox_gates (
  inbox_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  gate_id         TEXT NOT NULL,
  feature         TEXT NOT NULL,
  phase           TEXT,
  context_json    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected')),
  review_note     TEXT,
  enqueued_at     INTEGER NOT NULL,
  resolved_at     INTEGER,
  blocks_dispatch_for TEXT
);
CREATE INDEX IF NOT EXISTS idx_mo_inbox_gates_pending
  ON mo_inbox_gates(status) WHERE status='pending';
"""

# The only statuses a resolution may write. 'pending' is the pre-resolution
# state, not an outcome — the DDL CHECK rejects it, and so does resolve().
RESOLVABLE = ("approved", "rejected")


def _resolve_db_path(db_path: str | os.PathLike[str] | None = None) -> str:
    if db_path is not None:
        return os.fspath(db_path)
    return os.environ.get("MINI_ORK_DB") or ".mini-ork/state.db"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_BOOTSTRAP_SQL)
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Full row as a dict, with ``context`` decoded from ``context_json``."""
    out = dict(row)
    raw = out.pop("context_json", None)
    try:
        out["context"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        out["context"] = {}
    return out


def enqueue(
    gate_id: str,
    feature: str,
    phase: str = "",
    context: dict | None = None,
    *,
    blocks_dispatch_for: str = "",
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    """Insert a ``pending`` item; return its new ``inbox_id``.

    Raises ``ValueError`` when ``gate_id`` or ``feature`` is empty — an
    oversight item that names no gate and no feature cannot be routed, counted,
    or resolved.
    """
    if not gate_id or not gate_id.strip():
        raise ValueError("gate_id must be a non-empty string")
    if not feature or not feature.strip():
        raise ValueError("feature must be a non-empty string")

    conn = _connect(_resolve_db_path(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO mo_inbox_gates "
            "(gate_id, feature, phase, context_json, status, enqueued_at, "
            " blocks_dispatch_for) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (gate_id, feature, phase, json.dumps(context or {}),
             time.time(), blocks_dispatch_for),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def resolve(
    inbox_id: int,
    status: str,
    *,
    review_note: str = "",
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Close an item as ``approved`` or ``rejected``.

    Returns ``True`` iff the row existed **and was still pending** — a second
    resolve returns ``False`` and changes nothing, so the first note and the
    first resolution timestamp are the ones that survive. Raises ``ValueError``
    for any other ``status``, including ``pending``: "pending" is not an
    outcome, and silently accepting it would let a no-op read as a resolution.
    """
    if status not in RESOLVABLE:
        raise ValueError(
            f"status must be one of {RESOLVABLE!r}, got {status!r}"
        )

    conn = _connect(_resolve_db_path(db_path))
    try:
        cur = conn.execute(
            "UPDATE mo_inbox_gates "
            "   SET status = ?, resolved_at = ?, review_note = ? "
            " WHERE inbox_id = ? AND status = 'pending'",
            (status, time.time(), review_note, inbox_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def pending(
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> list[dict]:
    """Every ``status='pending'`` row, oldest first, ``context`` decoded."""
    conn = _connect(_resolve_db_path(db_path))
    try:
        rows = conn.execute(
            "SELECT * FROM mo_inbox_gates WHERE status = 'pending' "
            "ORDER BY enqueued_at ASC, inbox_id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get(
    inbox_id: int,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> dict | None:
    """The full row as a dict (``context`` decoded), or ``None`` if unknown."""
    conn = _connect(_resolve_db_path(db_path))
    try:
        row = conn.execute(
            "SELECT * FROM mo_inbox_gates WHERE inbox_id = ?", (inbox_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row is not None else None
