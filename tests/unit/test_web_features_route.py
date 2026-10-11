"""Tests for the read-only feature catalogue route (``GET /api/v1/features``).

Covers the registry-backed listing the wizard and IDE read:

* AC1 — 200 and an exact match of ``catalogue_json()`` (``premium_threshold``
  float + ``features`` list).
* AC2 — ``?recipe=`` is forwarded to ``catalogue_json(recipe)`` and observably
  narrows: ``recursion`` appears for ``recursive-self-improve`` and not for
  ``code-fix``.
* AC3 — the read is unauthenticated: it succeeds with no credentials and the
  route declares no dependencies.

The route never touches ``state.db``, so the app boots against a bare tmp home.
The app is built via ``create_app`` (AC4/AC5) and driven by TestClient, mirroring
tests/unit/test_artifacts_route_py.py's httpx availability guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# TestClient needs an httpx backend: newer starlette hard-requires httpx2,
# older starlette works with httpx (1). CI installs httpx2; local zero-dep
# runs may have neither — skip cleanly instead of erroring at collection.
try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as _exc:  # RuntimeError: starlette>=httpx2-only
    TestClient = None
    pytestmark = pytest.mark.skip(
        reason=f"starlette TestClient unavailable (needs httpx2 or httpx): {_exc}")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


@pytest.fixture
def client(tmp_path: Path):
    from mini_ork.web.app import create_app

    # The features route is registry-only: an empty home is a valid one.
    home = tmp_path / ".mini-ork"
    home.mkdir()
    return TestClient(create_app(home=home, dev_cors=False))


def test_listing_matches_catalogue_json(client) -> None:
    from mini_ork.features import catalogue_json

    resp = client.get("/api/v1/features")
    assert resp.status_code == 200
    body = resp.json()
    assert body == catalogue_json()  # verbatim, not re-derived
    assert isinstance(body["premium_threshold"], float)
    assert isinstance(body["features"], list)
    assert body["features"]  # built-ins are seeded at import
    assert {"id", "label", "kind", "tier", "cost_multiplier", "applies_to"} <= set(
        body["features"][0]
    )


def test_recipe_narrows_like_catalogue_json(client) -> None:
    from mini_ork.features import catalogue_json

    code_fix = client.get("/api/v1/features", params={"recipe": "code-fix"})
    assert code_fix.status_code == 200
    assert code_fix.json() == catalogue_json("code-fix")

    recursive = client.get("/api/v1/features", params={"recipe": "recursive-self-improve"})
    assert recursive.status_code == 200
    assert recursive.json() == catalogue_json("recursive-self-improve")

    # The observable narrowing: the recipe-scoped `recursion` feature is only in
    # the recursive-self-improve set, never the code-fix one.
    assert "recursion" in {f["id"] for f in recursive.json()["features"]}
    assert "recursion" not in {f["id"] for f in code_fix.json()["features"]}


def test_read_is_unauthenticated_and_get_only(client) -> None:
    # No Authorization header / cookie is sent, and none is needed.
    resp = client.get("/api/v1/features")
    assert resp.status_code == 200
    assert "authorization" not in {k.lower() for k in resp.request.headers}


def test_route_declares_no_dependencies() -> None:
    """AC3 at the route level: no auth dependency, GET only, no write verb."""
    from mini_ork.web.routes import features

    route = next(
        r
        for r in features.router.routes
        if getattr(r, "path", None) == "/api/v1/features"
    )
    assert route.dependencies == []
    assert route.methods == {"GET"}
