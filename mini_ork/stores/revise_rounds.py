"""Rounds-to-pass ledger for the revise loop (rev6 R5b).

Schema: ``db/migrations/0081_revise_rounds.sql``. One row per attempt of a
revise target: round 0 is the first attempt, round n the n-th revise round.
``passed`` says whether the attempt ended with every gate green (NULL means the
attempt was granted but never settled).

The executor's round counter is in memory, so a ``recover`` process starts it
again at 1. Callers therefore number rows from :func:`next_round` (an offset
read from the table) so a recovered run continues its numbering instead of
overwriting it.

The writer must never change a run's outcome: every public function returns a
neutral value (``0`` for :func:`next_round`, ``None`` for the writers) when
``db`` is empty, is not a file, or lacks the table, and on any
``sqlite3.Error``. ``sqlite3.connect`` would *create* a missing path, so the
file check happens before connecting.

``passed`` is run-level, not target-level: the executor writes it from its
global failure count, so it means "the run was green when this function
returned", not "this target's own gate passed".
"""
from __future__ import annotations

import os
import sqlite3
import time

__all__ = ["next_round", "record_grant", "record_outcome"]

_BUSY_TIMEOUT_S = 15
_BUSY_MS = 15000

# created_at is deliberately absent from the UPDATE: the first insert's value
# stays. A later NULL cost keeps the earlier non-NULL one.
_UPSERT = (
    "INSERT INTO revise_rounds"
    " (run_id, task_class, target_node, round, passed, cost_usd, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(run_id, target_node, round) DO UPDATE SET"
    " passed = excluded.passed,"
    " cost_usd = COALESCE(excluded.cost_usd, revise_rounds.cost_usd)"
)

# record_grant's *starting* row: an existing row (a crashed process may have
# left round N unsettled) is left exactly as it was.
_INSERT_IGNORE = (
    "INSERT OR IGNORE INTO revise_rounds"
    " (run_id, task_class, target_node, round, passed, cost_usd, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _open(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=_BUSY_TIMEOUT_S)
    con.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
    return con


def _usable(db: str) -> bool:
    return bool(db) and os.path.isfile(db)


def _now(now: int | None) -> int:
    return int(now) if now is not None else int(time.time())


def next_round(db: str, run_id: str, target: str) -> int:
    """``MAX(round) + 1`` for ``(run_id, target)``, or 0 when there are none."""
    if not _usable(db):
        return 0
    try:
        con = _open(db)
    except sqlite3.Error:
        return 0
    try:
        row = con.execute(
            "SELECT MAX(round) FROM revise_rounds WHERE run_id = ? AND target_node = ?",
            (run_id, target),
        ).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        con.close()
    return 0 if not row or row[0] is None else int(row[0]) + 1


def record_grant(
    db: str,
    run_id: str,
    task_class: str | None,
    target: str,
    db_round: int,
    cost_usd: float | None = None,
    now: int | None = None,
) -> None:
    """Attempt ``db_round - 1`` failed and attempt ``db_round`` starts.

    Upserts row ``db_round - 1`` with ``passed = 0`` and inserts row
    ``db_round`` with ``passed = NULL`` (an existing starting row is kept).
    """
    if not _usable(db):
        return
    at = _now(now)
    try:
        con = _open(db)
    except sqlite3.Error:
        return
    try:
        with con:
            con.execute(
                _UPSERT,
                (run_id, task_class, target, db_round - 1, 0, cost_usd, at),
            )
            con.execute(
                _INSERT_IGNORE,
                (run_id, task_class, target, db_round, None, cost_usd, at),
            )
    except sqlite3.Error:
        return
    finally:
        con.close()


def record_outcome(
    db: str,
    run_id: str,
    task_class: str | None,
    target: str,
    db_round: int,
    passed: bool,
    cost_usd: float | None = None,
    now: int | None = None,
) -> None:
    """Upsert row ``db_round`` with ``passed = 1 if passed else 0``."""
    if not _usable(db):
        return
    at = _now(now)
    try:
        con = _open(db)
    except sqlite3.Error:
        return
    try:
        with con:
            con.execute(
                _UPSERT,
                (run_id, task_class, target, db_round, 1 if passed else 0, cost_usd, at),
            )
    except sqlite3.Error:
        return
    finally:
        con.close()
