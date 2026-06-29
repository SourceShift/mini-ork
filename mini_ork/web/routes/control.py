"""POST endpoints for task_run lifecycle control (stop/kill/pause/resume).

Read-write boundary. Bound to 127.0.0.1 only by default — see
mini_ork/web/control.py for the security note.

The /pause-cost + /resume-cost endpoints require Bearer-token auth
via mini_ork.web.auth.require_token (Epic E6 substrate). When
${MINI_ORK_HOME}/auth-tokens.txt is missing, all auth-gated POSTs
return 401. The legacy /stop and /kill endpoints stay
loopback-trust-based for backwards compatibility; tighten by
adding the same dependency when ready.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path as PathParam

from .. import auth, control
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


# ── cost-pause / resume (E4 + E6 substrate, Bearer-token auth required) ──


@router.post("/{task_run_id}/pause-cost")
def pause_cost(
    task_run_id: str = PathParam(...),
    threshold_usd: float = Body(
        25.0,
        embed=True,
        description=(
            "Cost-window threshold (USD) stamped into the sentinel. "
            "Stored for audit; the dispatcher's MO_PAUSE_EVERY_USD "
            "controls when the next pause fires."
        ),
    ),
    home=Depends(get_home),
    operator: str = Depends(auth.require_token),
) -> dict[str, Any]:
    """Touch the cost-pause sentinel so the dispatcher pauses before
    its next LLM call. Mirrors the lib/cost_pause.sh sentinel shape."""
    result = control.pause_cost_run(home, task_run_id, threshold_usd)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "pause failed"))
    result["operator"] = operator
    return result


@router.post("/{task_run_id}/resume-cost")
def resume_cost(
    task_run_id: str = PathParam(...),
    home=Depends(get_home),
    operator: str = Depends(auth.require_token),
) -> dict[str, Any]:
    """Clear the cost-pause sentinel + record the approval. Audit row
    lands at .cost-pause-approvals.jsonl with operator + timestamp."""
    result = control.resume_cost_run(home, task_run_id, approver=operator)
    if not result.get("ok"):
        # 409 because the sentinel might already be absent — "nothing to resume"
        # is a different error class than "run doesn't exist".
        raise HTTPException(status_code=409, detail=result.get("error", "resume failed"))
    return result


# ── HITL steering injection (U2, Bearer-token auth required) ──────────────


@router.post("/{task_run_id}/steer")
def steer(
    task_run_id: str = PathParam(...),
    message: str = Body(..., embed=True, description="Steering text the agent sees next"),
    role_target: str = Body("any", embed=True, description="planner/implementer/reviewer/verifier/any"),
    severity: str = Body("info", embed=True, description="info/warn/critical"),
    confidence: float = Body(0.8, embed=True, description="0.0–1.0; context_assembler ranks by this"),
    ttl_secs: int = Body(3600, embed=True, description="message TTL in seconds"),
    db: StateDB = Depends(get_db),
    operator: str = Depends(auth.require_token),
) -> dict[str, Any]:
    """Inject an operator-steering message into an in-flight run. The targeted
    agent role picks it up on its next context_assemble pack (the HTTP-write
    counterpart to lib/operator_steering.sh + the read-side MCP tool)."""
    result = control.steer_run(
        db,
        task_run_id,
        message,
        role_target=role_target,
        severity=severity,
        source=f"http:{operator}",
        confidence=confidence,
        ttl_secs=ttl_secs,
    )
    if not result.get("ok"):
        err = result.get("error", "steer failed")
        status = 404 if "not found" in err else 400
        raise HTTPException(status_code=status, detail=err)
    result["operator"] = operator
    return result
