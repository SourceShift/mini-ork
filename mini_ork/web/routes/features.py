"""Read-only feature catalogue: the controllable-feature registry over HTTP.

``GET /api/v1/features`` returns ``mini_ork.features.catalogue_json()`` verbatim
— the registry stays the single source of truth, so this surface re-derives
nothing (no hand-kept list to drift). The optional ``?recipe=`` query parameter
narrows to that recipe's features, exactly as the ``mini-ork features`` CLI does.

Read-only on purpose: no auth and no database. The registry is process memory,
seeded by ``_load_builtins`` at import time, so the endpoint answers even against
an empty ``.mini-ork`` home — a missing ``state.db`` is not a missing feature set.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from mini_ork.features import catalogue_json

router = APIRouter(prefix="/api/v1", tags=["features"])


@router.get("/features")
def list_features(recipe: str | None = None) -> dict[str, Any]:
    """The feature catalogue as JSON; ``?recipe=`` narrows to that recipe's set."""
    return catalogue_json(recipe)
