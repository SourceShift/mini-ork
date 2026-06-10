"""Trajectory page data: self-improve convergence, cost trend, finding-rate."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..db import StateDB
from ..deps import get_db

router = APIRouter(prefix="/api/v1/trajectory", tags=["trajectory"])


@router.get("/self-improve")
def self_improve_runs(db: StateDB = Depends(get_db), limit: int = 100) -> list[dict[str, Any]]:
    if not db.has_table("self_improve_runs"):
        return []
    return db.rows(
        """
        SELECT run_id, iter, outcome, started_at, finished_at,
               soft_deadline_at, hard_deadline_at, parent_run_id,
               branch_name, worktree_path, notes
        FROM self_improve_runs
        ORDER BY iter DESC
        LIMIT ?
        """,
        (limit,),
    )


@router.get("/self-improve/{run_id}")
def self_improve_detail(
    run_id: str,
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    """Single self_improve_run + parsed notes + linked task_run if present.

    self_improve_runs.notes is conventionally a semicolon-separated list
    of key=value pairs (e.g. "starting;base_ref=main@SHA;base_ref_fallback=0").
    We parse it client-side too, but the structured view here lets the
    UI show typed values (booleans, sha-shortening) consistently.
    """
    if not db.has_table("self_improve_runs"):
        raise HTTPException(status_code=404, detail="self_improve_runs table missing")
    row = db.row(
        "SELECT * FROM self_improve_runs WHERE run_id = ?",
        (run_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"self_improve run {run_id} not found")

    parsed_notes = _parse_kv_notes(row.get("notes"))

    # Walk descendants — many self_improve_runs reference each other via parent_run_id
    children = db.rows(
        """
        SELECT run_id, iter, outcome, started_at, finished_at
        FROM self_improve_runs WHERE parent_run_id = ?
        ORDER BY iter ASC
        """,
        (run_id,),
    )

    # Linked task_run: the naming convention is "self-improve-iter-{iter}-{timestamp}"
    # The run_id of self_improve_runs is the same string by design.
    linked_task_run = None
    if db.has_table("task_runs"):
        linked_task_run = db.row(
            "SELECT id, recipe, status, verdict, cost_usd, duration_ms, created_at, trace_id, plan_path, artifact_path FROM task_runs WHERE id = ?",
            (run_id,),
        )

    # Sibling iterations for context (prev + next)
    siblings = db.rows(
        """
        SELECT run_id, iter, outcome
        FROM self_improve_runs
        WHERE iter BETWEEN ? AND ?
        ORDER BY iter ASC
        """,
        (int(row["iter"]) - 2, int(row["iter"]) + 2),
    )

    return {
        **row,
        "parsed_notes": parsed_notes,
        "children": children,
        "siblings": siblings,
        "linked_task_run": linked_task_run,
    }


def _parse_kv_notes(notes: str | None) -> list[dict[str, str]]:
    """Parse "starting;base_ref=main@SHA;flag=1" → [{key,value,kind}, ...].

    Kind is a coarse hint for client rendering: 'flag' for bare tokens
    (no '='), 'kv' for key=value pairs, 'sha' when value looks like a
    git ref with @ commit hash.
    """
    if not notes:
        return []
    out: list[dict[str, str]] = []
    for raw in (s.strip() for s in str(notes).split(";")):
        if not raw:
            continue
        if "=" not in raw:
            out.append({"key": raw, "value": "", "kind": "flag"})
            continue
        key, _, value = raw.partition("=")
        kind = "sha" if "@" in value and len(value.rsplit("@", 1)[-1]) >= 7 else "kv"
        out.append({"key": key.strip(), "value": value.strip(), "kind": kind})
    return out


@router.get("/cost-by-day")
def cost_by_day(db: StateDB = Depends(get_db)) -> list[dict[str, Any]]:
    """Stacked-area data: cost_usd per day per recipe."""
    if not db.has_table("task_runs"):
        return []
    return db.rows(
        """
        SELECT date(datetime(created_at, 'unixepoch')) AS day,
               COALESCE(recipe, 'unspecified') AS recipe,
               SUM(cost_usd) AS cost,
               COUNT(*) AS run_count
        FROM task_runs
        GROUP BY day, recipe
        ORDER BY day ASC, recipe ASC
        """
    )


@router.get("/wall-time")
def wall_time(db: StateDB = Depends(get_db)) -> list[dict[str, Any]]:
    """Median wall-time per recipe over time."""
    if not db.has_table("task_runs"):
        return []
    # duration_ms was historically never written by the engine (always 0),
    # so derive wall time from ended_at - created_at when the column is empty.
    return db.rows(
        """
        SELECT day, recipe,
               AVG(eff_ms) AS avg_ms,
               MAX(eff_ms) AS max_ms,
               COUNT(*) AS run_count
        FROM (
            SELECT date(datetime(created_at, 'unixepoch')) AS day,
                   COALESCE(recipe, 'unspecified') AS recipe,
                   COALESCE(NULLIF(duration_ms, 0),
                            CASE WHEN ended_at IS NOT NULL
                                 THEN MAX(ended_at - created_at, 0) * 1000 END,
                            0) AS eff_ms
            FROM task_runs
        )
        WHERE eff_ms > 0
        GROUP BY day, recipe
        ORDER BY day ASC
        """
    )


@router.get("/gradients")
def gradients(db: StateDB = Depends(get_db), limit: int = 50) -> list[dict[str, Any]]:
    if not db.has_table("gradient_records"):
        return []
    return db.rows(
        """
        SELECT gradient_id, target, signal, suggested_change, confidence, created_at
        FROM gradient_records
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
