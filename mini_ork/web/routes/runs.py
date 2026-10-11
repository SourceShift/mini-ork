"""POST /api/v1/runs — detached run launch (U1).

Non-blocking counterpart to ``mini_ork.client.MiniOrk.run`` (which blocks up to
1800s for the run to finish). Returns a ``run_id`` immediately; the caller then
observes progress via ``GET /api/v1/stream?task_run=<id>`` and drives the run via
``POST /api/v1/task-runs/{id}/steer`` + ``/stop``.

Bearer-token auth required (``mini_ork.web.auth.require_token``) — launching a run
is a privileged write; fail-closed when ``auth-tokens.txt`` is absent.

``POST /api/v1/runs/plan`` is the read-only counterpart: it compiles the recipe's
workflow and reports the node DAG + feature cost without auth, a DB handle, or any
side effect (see its docstring).
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from mini_ork.features import effective_multiplier, premium_unselected, resolve_selection
from mini_ork.workflow import WorkflowCompileError, compile_workflow

from .. import auth, control
from ..deps import get_home

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


@router.post("")
def launch(
    recipe: str = Body(..., embed=True, description="Recipe name, e.g. 'code-fix'"),
    kickoff_markdown: str = Body(..., embed=True, description="Kickoff markdown body"),
    run_id: str | None = Body(None, embed=True, description="Optional caller-supplied run id"),
    extra_env: dict[str, str] | None = Body(
        None,
        embed=True,
        description=(
            "Per-run env overrides — the wizard's feature selection. Premium-feature "
            "knobs are stripped unless MO_ACCEPT_PREMIUM=1 is included "
            "(see `mini-ork features`)."
        ),
    ),
    home=Depends(get_home),
    operator: str = Depends(auth.require_token),
) -> dict[str, Any]:
    """Launch a recipe detached and return its run_id without blocking."""
    result = control.launch_run(
        home, recipe, kickoff_markdown, run_id=run_id, extra_env=extra_env
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "launch failed"))
    result["operator"] = operator
    return result


@router.post("/plan")
def plan(
    recipe: str = Body(..., embed=True, description="Recipe name, e.g. 'code-fix'"),
    features: dict[str, str] | None = Body(
        None,
        embed=True,
        description=(
            "Feature selection (feature id → value) — the wizard's current picks. "
            "Reported back as env/blocked/cost_multiplier; nothing is launched."
        ),
    ),
    accept_premium: bool = Body(
        False,
        embed=True,
        description=(
            "Treat premium features as accepted (mirrors MO_ACCEPT_PREMIUM). When "
            "false, premium ids are listed in `features.blocked`."
        ),
    ),
) -> dict[str, Any]:
    """Compile a recipe and preview its node DAG + feature cost — read-only.

    No ``Depends(auth.require_token)`` and no ``Depends(get_db)``: this reads
    recipe YAML and the feature registry only. It never launches a run, never
    writes a ``task_runs`` row, and never persists the plan, so the wizard can
    call it on every feature toggle without a privileged token or a side effect.
    """
    # Lazy import: the recipe-base resolver lives on the heavy CLI module. Import
    # it here (not at web-app import time) so the route resolves recipes through
    # the exact same MINI_ORK_HOME/cwd/root search the CLI uses — no re-implemented
    # probe, no hardcoded recipes path.
    from mini_ork.cli.main import _resolve_recipe_base

    root = str(control._mini_ork_root())
    recipe_base, resolved = _resolve_recipe_base(root, recipe)
    if not recipe_base:
        raise HTTPException(status_code=400, detail=f"no recipe: {recipe}")
    workflow_path = os.path.join(recipe_base, "recipes", resolved, "workflow.yaml")
    try:
        compiled = compile_workflow(workflow_path)
    except WorkflowCompileError as exc:
        # A missing/corrupt workflow is a bad request, never a 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    selection = features or {}
    blocked = [] if accept_premium else [f.id for f in premium_unselected(selection)]
    nodes = [
        {
            "name": compiled.nodes[node_id].name,
            "type": compiled.nodes[node_id].type,
            "lane": compiled.nodes[node_id].model_lane,
        }
        for node_id in compiled.topological_order
    ]
    return {
        "recipe": resolved,
        "nodes": nodes,
        "topological_order": list(compiled.topological_order),
        "features": {
            "env": resolve_selection(selection, accept_premium=accept_premium),
            "blocked": blocked,
            "cost_multiplier": effective_multiplier(selection),
        },
    }
