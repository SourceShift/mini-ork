"""Detection-fingerprint view — the framework's load-bearing receipts.

Per recipe: which families ran which lens? Is this a heterogeneous panel
or a same-family coalition? Drives the "list the model families behind
every hunter and every validator" call-out from docs/positioning.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import recipes

router = APIRouter(prefix="/api/v1/fingerprint", tags=["fingerprint"])


@router.get("/recipes")
def list_recipes() -> list[str]:
    return recipes.list_recipes()


@router.get("/lanes")
def lanes() -> dict[str, str]:
    return recipes.load_lanes()


@router.get("")
def fingerprint(recipe: str = Query(..., description="recipe directory name")) -> dict[str, Any]:
    try:
        return recipes.fingerprint(recipe)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"recipe load failed: {e}")
