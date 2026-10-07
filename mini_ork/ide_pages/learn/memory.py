"""Memory tab — Namespaces and Semantic memory lifecycle.

The Idea tree moved to ``improve.py`` because it answers "what to harvest next"
rather than "what is in the store".
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import _NAMESPACES, count, db


def sections(home: Path, args: dict[str, str], errors: dict[str, str]) -> list[dict[str, Any]]:
    return (S.guarded(errors, "Namespaces · state.db", lambda: _namespaces(home))
            + S.guarded(errors, "Semantic memory lifecycle", lambda: _lifecycle(home)))


def _namespaces(home: Path) -> dict[str, Any]:
    conn = db(home)
    counts = [(label, count(conn, table)) for label, table in _NAMESPACES]
    top = max((n for _, n in counts), default=0) or 1
    return S.bars("Namespaces · state.db", [(label, 100 * n / top, f"{n:,}") for label, n in counts],
                  full=True)


def _lifecycle(home: Path) -> dict[str, Any]:
    from mini_ork.memory import RETIRE_ENTER_UTILITY, RETIRE_MIN_USES

    conn = db(home)
    if not conn.has_table("semantic_memory"):
        return S.kv("Semantic memory lifecycle", [("Active", "0"), ("Decaying", "0", "yellow"),
                                                  ("Retired", "0", "sub")])
    # The win/loss and retirement columns are added by mini_ork.memory.semantic on
    # first use; a store that never ran it has neither, so every row is active.
    cols = {r["name"] for r in conn.rows("PRAGMA table_info(semantic_memory)")}
    retired = "COALESCE(retired_at,0)" if "retired_at" in cols else "0"
    weak = ("uses >= ? AND (wins + 1.0) / (uses + 2.0) < ?" if {"uses", "wins"} <= cols else "0 = 1")
    params = (RETIRE_MIN_USES, RETIRE_ENTER_UTILITY) if {"uses", "wins"} <= cols else ()
    row = conn.rows(
        f"SELECT COUNT(*) AS total, SUM(CASE WHEN {retired} = 0 THEN 1 ELSE 0 END) AS active, "  # noqa: S608
        f"SUM(CASE WHEN {retired} = 0 AND {weak} THEN 1 ELSE 0 END) AS decaying, "
        f"SUM(CASE WHEN {retired} != 0 THEN 1 ELSE 0 END) AS retired FROM semantic_memory",
        params)[0]
    active, decaying = int(row.get("active") or 0), int(row.get("decaying") or 0)
    return S.kv("Semantic memory lifecycle", [
        ("Active", f"{active - decaying:,}"), ("Decaying", f"{decaying:,}", "yellow",
                                               "retirement candidates"),
        ("Retired", f"{int(row.get('retired') or 0):,}", "sub")],
        note="Decaying: active, used at least "
             f"{RETIRE_MIN_USES}×, win rate under {RETIRE_ENTER_UTILITY:.2f}. Nothing retires on its own.")