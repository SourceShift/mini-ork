"""Read-only DB reader for the diversity / pass@k collapse floor.

Feeds :func:`mini_ork.learning.collapse_detector.floor` from outcomes that
already exist in ``state.db``: terminal ``task_runs`` (published = passed) and
the implementer lanes recorded in ``execution_traces``. This module never
writes or commits — it is a reader; the decision to act belongs to the caller.

No network, no model. A missing table or column yields an empty history rather
than an error, so an unmigrated DB reads as "insufficient history", not a
crash.
"""
from __future__ import annotations

import os
import sqlite3

from mini_ork.learning import collapse_detector

_RUNS_SQL = (
    "SELECT id, status, kickoff_path FROM task_runs "
    "WHERE task_class=? AND status IN ('published','failed') "
    "ORDER BY created_at DESC, id DESC LIMIT ?"
)


def resolve_window() -> int:
    """Floor window from ``MO_CB_FLOOR_WINDOW`` when it is an int >= 1."""
    raw = os.environ.get("MO_CB_FLOOR_WINDOW")
    if raw is not None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return collapse_detector.FLOOR_WINDOW
        if value >= 1:
            return value
    return collapse_detector.FLOOR_WINDOW


def floor_runs(
    con: sqlite3.Connection, task_class: str, *, limit: int
) -> list[dict]:
    """Return the newest ``limit`` terminal runs for ``task_class``, oldest-first.

    Each row is ``{"run_id", "passed", "task_key", "lanes"}``; ``lanes`` holds
    the sorted distinct implementer lane ids seen for that run.
    """
    try:
        rows = con.execute(_RUNS_SQL, (task_class, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []

    # Query returns newest-first; the floor expects oldest-first.
    rows = list(reversed(rows))
    ids = [str(row[0]) for row in rows]

    lanes_by_run: dict[str, set[str]] = {run_id: set() for run_id in ids}
    placeholders = ",".join("?" for _ in ids)
    try:
        traces = con.execute(
            "SELECT run_id, agent_version_id FROM execution_traces "
            f"WHERE run_id IN ({placeholders}) "
            "AND trace_id LIKE 'tr-implementer-%' "
            "AND TRIM(COALESCE(agent_version_id,'')) <> ''",
            ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    for trace in traces:
        run_id = str(trace[0])
        lane = str(trace[1]).strip()
        if lane and run_id in lanes_by_run:
            lanes_by_run[run_id].add(lane)

    return [
        {
            "run_id": str(row[0]),
            "passed": row[1] == "published",
            "task_key": row[2] or "",
            "lanes": sorted(lanes_by_run[str(row[0])]),
        }
        for row in rows
    ]


def evaluate(
    con: sqlite3.Connection, task_class: str, *, window: int | None = None
) -> dict:
    """Evaluate the floor for ``task_class``; ``report["task_class"]`` is set."""
    w = max(1, int(window if window is not None else resolve_window()))
    report = collapse_detector.floor(
        floor_runs(con, task_class, limit=2 * w), window=w
    )
    report["task_class"] = task_class
    return report
