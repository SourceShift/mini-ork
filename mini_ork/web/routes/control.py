"""POST endpoints for task_run lifecycle control (stop/kill).

Read-write boundary. Bound to 127.0.0.1 only by default — see
mini_ork/web/control.py for the security note.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path as PathParam

from .. import control
from ..db import StateDB
from ..deps import get_db, get_home

router = APIRouter(prefix="/api/v1/task-runs", tags=["control"])


@router.post("/{task_run_id}/stop")
def stop(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
    home=Depends(get_home),
) -> dict[str, Any]:
    result = control.stop_run(home, db, task_run_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "stop failed"))
    return result


@router.post("/{task_run_id}/kill")
def kill(
    task_run_id: str = PathParam(...),
    db: StateDB = Depends(get_db),
    home=Depends(get_home),
) -> dict[str, Any]:
    result = control.kill_run(home, db, task_run_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "kill failed"))
    return result


# ── interactive Q&A for planner needs_answers ────────────────────────────


@router.get("/{task_run_id}/profile")
def get_profile_route(
    task_run_id: str = PathParam(...),
    home=Depends(get_home),
) -> dict[str, Any]:
    """Read the planner's run_profile.json — exposes human_questions for the
    UI's interactive Q&A panel. Returns needs_answers=True when the user
    should be prompted."""
    return control.get_profile(home, task_run_id)


@router.post("/{task_run_id}/answers")
def save_answers_route(
    task_run_id: str = PathParam(...),
    answers: dict[str, str] = Body(..., description="Map of question → answer"),
    home=Depends(get_home),
) -> dict[str, Any]:
    """Accept user answers to planner questions; persist + suggest next CLI."""
    if not answers:
        raise HTTPException(status_code=400, detail="empty answers dict")
    return control.save_answers(home, task_run_id, answers)
