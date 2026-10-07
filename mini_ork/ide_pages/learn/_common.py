"""Shared helpers for the ``learn`` page tab modules.

This is the cross-module source of truth: every tab module imports from here so
the helpers stay consistent. The names mirror the ones that used to live in
``learn.py`` so the body code stays unchanged during the package split.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# The state.db namespaces the design's "Namespaces" bars count.
_NAMESPACES = [("task", "task_memory"), ("workflow", "workflow_memory"),
               ("agent_performance", "agent_performance_memory"), ("failure", "failure_memory"),
               ("recovery", "recovery_memory"), ("user_preference", "user_preference_memory"),
               ("artifact", "artifact_memory"), ("benchmark", "benchmark_memory")]
_GOOD = {"success", "promoted", "adopted", "harvested", "accepted", "approved"}
_BAD = {"rejected", "failed", "timed_out", "refuted", "pruned", "quarantined", "error"}


def db(home: Path):
    """Lazy DB handle — pages must build on empty homes where ``state.db`` may not exist yet."""
    from mini_ork.web.db import db_for

    return db_for(home)


def count(db, table: str) -> int:
    """Count rows of ``table``; 0 when the table is absent."""
    if not db.has_table(table):
        return 0
    rows = db.rows(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 — fixed table names
    return int(rows[0]["n"]) if rows else 0


def short(text: Any, limit: int = 120) -> str:
    """Collapse whitespace and truncate with an ellipsis when over ``limit``."""
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def outcome_colour(value: Any) -> str:
    """Return the page colour name (green / red / yellow / sub) for an outcome string."""
    v = str(value or "").lower()
    if v in _GOOD:
        return "green"
    if v in _BAD:
        return "red"
    return "yellow" if v else "sub"