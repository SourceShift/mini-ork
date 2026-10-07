"""Lessons tab — Gradients, Patterns and Failure modes.

Moved from the legacy ``learn.py`` unchanged. A later phase replaces the
sections; until then, every section here is read-only and safe.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.learn._common import count, db, short


def sections(home: Path, args: dict[str, str], errors: dict[str, str]) -> list[dict[str, Any]]:
    return (S.guarded(errors, "Gradients", lambda: _gradients(home))
            + S.guarded(errors, "Patterns", lambda: _patterns(home))
            + S.guarded(errors, "Failure modes", lambda: _failure_modes(home)))


def _gradients(home: Path) -> dict[str, Any]:
    conn = db(home)
    cols = [S.col(fr=1, min=0), S.col(90), S.col(44), S.col(56)]
    head = ["gradient", "task class", "conf", "injected"]
    rows = conn.rows("SELECT gradient_id, target, signal, suggested_change, confidence, task_class "
                     "FROM gradient_records ORDER BY created_at DESC LIMIT 15") \
        if conn.has_table("gradient_records") else []
    out = [[S.cell(short(f"{r.get('target') or 'gradient'} · {r.get('signal') or r.get('suggested_change')}"),
                   "text"),
            S.muted(r.get("task_class") or "any"),
            S.mono(f"{float(r.get('confidence') or 0):.2f}"),
            S.muted("—")] for r in rows]
    if not out:
        out = [[S.muted("No gradients yet — reflection writes them as runs finish"), "", "", ""]]
    total = count(conn, "gradient_records")
    return S.table("Gradients", cols, head, out, full=True,
                   note=f"Reflection turns traces into natural-language gradients ({total:,} recorded, "
                        "newest first). High-confidence ones are injected into node prompts at dispatch; "
                        "injection is not counted per gradient.")


def _patterns(home: Path) -> dict[str, Any]:
    from mini_ork.web.routes.learning import emergent_patterns

    rows = emergent_patterns(db(home), 8)
    if not rows:
        return S.lst("Patterns", [S.dot("No patterns yet")])
    return S.lst("Patterns", [
        S.dot(short(r.get("cluster_label") or r.get("pattern_id"), 80),
              f"strength {float(r.get('strength_score') or 0):.2f} · {r.get('status') or 'open'}"
              + (f" · {short(r.get('suggested_meta_adr'), 90)}" if r.get("suggested_meta_adr") else ""))
        for r in rows])


def _failure_modes(home: Path) -> dict[str, Any]:
    """failure_memory grouped by category and stage, with the recovery that worked if one is recorded."""
    conn = db(home)
    rows = conn.rows(
        "SELECT failure_category AS category, workflow_stage AS stage, COUNT(*) AS n, "
        "COUNT(DISTINCT run_id) AS runs, MAX(occurred_at) AS last, MAX(failure_id) AS sample "
        "FROM failure_memory GROUP BY failure_category, workflow_stage ORDER BY n DESC LIMIT 6") \
        if conn.has_table("failure_memory") else []
    if not rows:
        return S.lst("Failure modes", [S.ok("No failure modes recorded")])
    recoveries: dict[str, str] = {}
    if conn.has_table("recovery_memory"):
        for r in conn.rows("SELECT f.failure_category AS category, m.recovery_action AS action "
                           "FROM recovery_memory m JOIN failure_memory f ON f.failure_id = m.failure_id "
                           "WHERE m.outcome = 'success' ORDER BY m.recovered_at DESC"):
            recoveries.setdefault(str(r.get("category")), str(r.get("action") or ""))
    items = []
    for r in rows:
        n, runs = int(r.get("n") or 0), int(r.get("runs") or 0)
        sub = f"{n} time{'s' if n != 1 else ''} in {runs} run{'s' if runs != 1 else ''} · last {str(r.get('last') or '—')[:10]}"
        if recoveries.get(str(r.get("category"))):
            sub += f" · recovery: {short(recoveries[str(r.get('category'))], 60)}"
        mark = S.bad if n >= 3 else S.warn
        items.append(mark(f"{r.get('category') or 'unknown'} · {r.get('stage') or 'any stage'}", sub))
    return S.lst("Failure modes", items)