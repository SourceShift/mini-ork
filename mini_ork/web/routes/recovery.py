"""Recovery view: project a (possibly recovered) run as ONE DAG (durable-dag E5).

Reads the E1–E3 durable tables (node_checkpoints, node_attempts,
recovery_requests, run_leases) via ``recovery_admin.recovery_projection`` — no
log scraping — so the UI renders completed nodes as completed, the failed node
with its failed attempt + next action, and new attempts nested beneath the
node, rather than a fresh unrelated run.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..deps import get_db
from ..db import StateDB
from ...ported.recovery_admin import recovery_projection

router = APIRouter(prefix="/api/v1", tags=["recovery"])


@router.get("/runs/{run_id}/recovery")
def recovery_view(run_id: str, db: StateDB = Depends(get_db)) -> dict[str, Any]:
    """DAG-shaped recovery projection for a run. Always returns a dict (empty
    nodes list if the run has no durable rows) so the UI can always render."""
    return recovery_projection(str(db.db_path), run_id)
