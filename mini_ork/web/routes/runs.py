"""POST /api/v1/runs — detached run launch (U1).

Non-blocking counterpart to ``mini_ork.client.MiniOrk.run`` (which blocks up to
1800s for the run to finish). Returns a ``run_id`` immediately; the caller then
observes progress via ``GET /api/v1/stream?task_run=<id>`` and drives the run via
``POST /api/v1/task-runs/{id}/steer`` + ``/stop``.

Bearer-token auth required (``mini_ork.web.auth.require_token``) — launching a run
is a privileged write; fail-closed when ``auth-tokens.txt`` is absent.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import auth, control
from ..deps import get_home

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


@router.post("")
def launch(
    recipe: str = Body(..., embed=True, description="Recipe name, e.g. 'code-fix'"),
    kickoff_markdown: str = Body(..., embed=True, description="Kickoff markdown body"),
    run_id: str | None = Body(None, embed=True, description="Optional caller-supplied run id"),
    home=Depends(get_home),
    operator: str = Depends(auth.require_token),
) -> dict[str, Any]:
    """Launch a recipe detached and return its run_id without blocking."""
    result = control.launch_run(home, recipe, kickoff_markdown, run_id=run_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "launch failed"))
    result["operator"] = operator
    return result
