"""The one writer of ``collapse_history`` rows — the loop's collapse signal.

``collapse_detector`` reads this table to notice a self-improving loop that is
climbing its own score while an independent anchor rots underneath it. Two
producers feed that table — the apply gate (``mini_ork/cli/apply.py``) and the
RSI cycle — and they share one schema, so the insert lives here rather than in
either caller. Two copies of the step arithmetic would drift, and a drifted step
sequence is precisely what the detector misreads as a collapse: a duplicated
step number makes two different rows look like one measurement.

The contract is fail-open, deliberately. Both callers are decision points
(``apply`` is finishing a promotion, a cycle is finishing a merge); neither may
fail because bookkeeping failed. A missing table (an old home that has not run
``mini-ork update``) or a write error returns ``None`` and leaves the caller's
decision untouched — the same shape ``circuit_breaker._eval_collapse_signal``
already uses for the read side.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

__all__ = ["append", "history"]

_STEP_SQL = (
    "SELECT COALESCE(MAX(step), -1) FROM collapse_history WHERE task_class = ?"
)


def append(
    con: sqlite3.Connection,
    *,
    task_class: str,
    score: float,
    anchor: float | None,
    directives: int = 0,
    run_id: str | None = None,
    now: int | None = None,
) -> int | None:
    """Append one measurement. Returns its ``step``, or ``None`` if not written.

    ``anchor is None`` means "this producer could not measure an anchor". A row
    with no anchor would teach the detector that the anchor had vanished, so it
    is not written at all — an absent measurement is not a zero.
    """
    if anchor is None:
        return None
    created = int(time.time() if now is None else now)
    try:
        row = con.execute(_STEP_SQL, (task_class,)).fetchone()
        step = (int(row[0]) + 1) if row and row[0] is not None else 0
        con.execute(
            "INSERT INTO collapse_history"
            " (task_class, step, score, anchor, directives, run_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_class,
                step,
                float(score),
                float(anchor),
                int(directives),
                run_id,
                created,
            ),
        )
        con.commit()
    except (sqlite3.Error, TypeError, ValueError):
        # The rollback is cleanup, not recovery: it is allowed to fail on a
        # connection that is already unusable, and that failure must not leak
        # out of a helper whose whole contract is "never raise".
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    return step


def history(
    con: sqlite3.Connection, task_class: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """The most recent rows, oldest first, in ``collapse_detector.detect`` shape.

    The detector only needs ``step``/``score``/``anchor`` and the *count* of
    directives per step, so the integer column is expanded into placeholder
    names here rather than stored as a list. Missing table → ``[]``.
    """
    try:
        rows = con.execute(
            "SELECT step, score, anchor, directives FROM collapse_history"
            " WHERE task_class = ? ORDER BY step DESC LIMIT ?",
            (task_class, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        out.append(
            {
                "step": int(row[0]),
                "score": float(row[1]),
                "anchor": float(row[2]),
                "directives": [f"d{i}" for i in range(int(row[3] or 0))],
            }
        )
    return out
