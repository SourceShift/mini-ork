"""Detection-fingerprint view — the framework's load-bearing receipts.

Per recipe: which families ran which lens? Is this a heterogeneous panel
or a same-family coalition? Drives the "list the model families behind
every hunter and every validator" call-out from docs/positioning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import recipes
from ..deps import get_home

router = APIRouter(prefix="/api/v1/fingerprint", tags=["fingerprint"])


@router.get("/recipes")
def list_recipes() -> list[str]:
    return recipes.list_recipes()


@router.get("/lanes")
def lanes(home: Path = Depends(get_home)) -> dict[str, str]:
    return recipes.load_lanes(home)


@router.get("")
def fingerprint(
    recipe: str = Query(..., description="recipe directory name"),
    home: Path = Depends(get_home),
) -> dict[str, Any]:
    try:
        return recipes.fingerprint(recipe, home)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"recipe load failed: {e}")
