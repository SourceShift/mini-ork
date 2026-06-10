"""Fleet view: active runs (heartbeat-tracked) + recent task_runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from ..deps import get_db
from ..db import StateDB

router = APIRouter(prefix="/api/v1", tags=["fleet"])


@router.get("/health")
def health(db: StateDB = Depends(get_db)) -> dict[str, Any]:
    return {
        "ok": True,
        "db_path": str(db.db_path),
        "has_task_runs": db.has_table("task_runs"),
        "has_mo_events": db.has_table("mo_events"),
        "has_self_improve_runs": db.has_table("self_improve_runs"),
    }


@router.get("/runs/active")
def active_runs(db: StateDB = Depends(get_db)) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    if db.has_table("runs"):
        for row in db.rows(
            """
            SELECT r.id, r.epic_id, r.run_dir, r.branch, r.agent, r.started_at,
                   r.last_heartbeat_at, r.pid, r.host, r.test_status, r.trace_status,
                   r.cost_usd, e.title AS epic_title, e.status AS epic_status, e.lane
            FROM runs r
            LEFT JOIN epics e ON e.id = r.epic_id
            WHERE r.ended_at IS NULL
            ORDER BY r.started_at DESC
            LIMIT 100
            """
        ):
            rows.append({**row, "source": "runs", "task_run_id": None, "status": None})

    if db.has_table("task_runs"):
        task_rows = db.rows(
            """
            SELECT id, parent_epic_id, task_class, recipe, status, verdict,
                   cost_usd, created_at, updated_at, ended_at
            FROM task_runs
            WHERE ended_at IS NULL
              AND status NOT IN ('published', 'rolled_back', 'failed')
            ORDER BY updated_at DESC
            LIMIT 100
            """
        )
        for row in task_rows:
            label = row.get("recipe") or row.get("task_class") or "task_run"
            rows.append(
                {
                    "id": row["id"],
                    "epic_id": row.get("parent_epic_id"),
                    "run_dir": f"runs/{row['id']}",
                    "branch": None,
                    "agent": label,
                    "started_at": row.get("created_at"),
                    "last_heartbeat_at": row.get("updated_at"),
                    "pid": None,
                    "host": None,
                    "test_status": row.get("status"),
                    "trace_status": row.get("verdict"),
                    "cost_usd": row.get("cost_usd"),
                    "epic_title": None,
                    "epic_status": None,
                    "lane": None,
                    "source": "task_runs",
                    "task_run_id": row["id"],
                    "task_class": row.get("task_class"),
                    "recipe": row.get("recipe"),
                    "status": row.get("status"),
                    "verdict": row.get("verdict"),
                }
            )

    rows.sort(
        key=lambda r: _active_sort_ts(r.get("last_heartbeat_at") or r.get("started_at")),
        reverse=True,
    )
    return rows[:100]


def _active_sort_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return 0.0
    return 0.0


@router.get("/task-runs")
def list_task_runs(
    db: StateDB = Depends(get_db),
    limit: int = Query(default=50, le=500),
    recipe: str | None = None,
    status: str | None = None,
    verdict: str | None = None,
) -> list[dict[str, Any]]:
    if not db.has_table("task_runs"):
        return []
    clauses = []
    params: list[Any] = []
    if recipe:
        clauses.append("recipe = ?")
        params.append(recipe)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if verdict:
        clauses.append("verdict = ?")
        params.append(verdict)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return db.rows(
        f"""
        SELECT id, task_class, recipe, workflow_version, status, verdict,
               cost_usd, duration_ms, created_at, updated_at, ended_at, trace_id,
               kickoff_path, plan_path, artifact_path
        FROM task_runs
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    )


@router.get("/task-runs/summary")
def task_runs_summary(db: StateDB = Depends(get_db)) -> dict[str, Any]:
    """Counts by recipe/status/verdict for the fleet header.

    Cached for 2s — the fleet view polls every 5s; counts don't change at
    sub-second resolution.
    """
    if not db.has_table("task_runs"):
        return {"by_recipe": [], "by_status": [], "total_cost_usd": 0.0}

    def _compute() -> dict[str, Any]:
        # All three aggregates share one connection (no per-query open/close).
        by_recipe = db.rows(
            """
            SELECT recipe, COUNT(*) AS count, SUM(cost_usd) AS cost
            FROM task_runs WHERE recipe IS NOT NULL
            GROUP BY recipe ORDER BY count DESC
            """
        )
        by_status = db.rows(
            "SELECT status, COUNT(*) AS count FROM task_runs GROUP BY status ORDER BY count DESC"
        )
        cost = db.row("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM task_runs")
        return {
            "by_recipe": by_recipe,
            "by_status": by_status,
            "total_cost_usd": cost["total"] if cost else 0.0,
        }

    return db.cached("task_runs_summary", ttl_s=2.0, producer=_compute)
