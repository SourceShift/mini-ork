"""Tests for POST /api/v1/runs/plan — the read-only workflow preview (wizard).

The endpoint compiles a recipe's workflow through the same resolver the CLI
uses, then reports the node DAG (topological order + per-node name/type/lane)
and a feature preview (env / blocked premium ids / cost multiplier). It is a
pure read: no auth dependency, no DB handle, no launch. The app is booted via
create_app against a tmp home and driven with the Starlette TestClient.
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

# A real in-repo recipe, so no fixture workflow needs authoring.
RECIPE = "code-fix"
# A premium (>1.5x) feature and the value that turns it on. Its multiplier is
# the lane count — "codex kimi glm" is 3 lanes, i.e. 3.0x.
PREMIUM_FEATURE = "review_panel"
PREMIUM_VALUE = "codex kimi glm"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mini_ork.web.app import create_app

    # Pin resolution to this checkout: no MINI_ORK_HOME overlay in play, root is
    # the repo, cwd is the repo. This mirrors the CLI's own probe order.
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.chdir(REPO)
    app = create_app(home=tmp_path / ".mini-ork", dev_cors=False)
    return TestClient(app)


def test_plan_200_shape(client) -> None:
    """AC1: 200 with recipe echo, node DAG in topological order, feature preview."""
    from mini_ork.workflow import compile_workflow

    resp = client.post("/api/v1/runs/plan", json={"recipe": RECIPE})
    assert resp.status_code == 200
    body = resp.json()

    assert body["recipe"] == RECIPE

    # topological_order matches the compiled workflow exactly, and nodes are the
    # same names in the same order.
    compiled = compile_workflow(REPO / "recipes" / RECIPE / "workflow.yaml")
    assert body["topological_order"] == list(compiled.topological_order)
    assert [n["name"] for n in body["nodes"]] == list(compiled.topological_order)

    # Every node carries exactly {name, type, lane}; lane echoes model_lane.
    for node in body["nodes"]:
        assert set(node.keys()) == {"name", "type", "lane"}
        src = compiled.nodes[node["name"]]
        assert node["type"] == src.type
        assert node["lane"] == src.model_lane

    # The feature preview is present with the three documented keys.
    feats = body["features"]
    assert set(feats.keys()) == {"env", "blocked", "cost_multiplier"}


def test_plan_no_auth_required(client) -> None:
    """AC5: the read route needs no bearer token (no Authorization header here)."""
    resp = client.post("/api/v1/runs/plan", json={"recipe": RECIPE})
    assert resp.status_code == 200


def test_plan_premium_raises_multiplier(client) -> None:
    """AC6: selecting a premium feature raises cost_multiplier and lists it blocked."""
    baseline = client.post("/api/v1/runs/plan", json={"recipe": RECIPE}).json()
    assert baseline["features"]["cost_multiplier"] == 1.0
    assert baseline["features"]["blocked"] == []

    premium = client.post(
        "/api/v1/runs/plan",
        json={"recipe": RECIPE, "features": {PREMIUM_FEATURE: PREMIUM_VALUE}},
    )
    assert premium.status_code == 200
    feats = premium.json()["features"]
    # 3 lanes -> 3.0x, strictly above the baseline.
    assert feats["cost_multiplier"] > baseline["features"]["cost_multiplier"]
    assert feats["cost_multiplier"] == 3.0
    # Not opted in: premium id is reported blocked and contributes no env.
    assert feats["blocked"] == [PREMIUM_FEATURE]
    assert feats["env"] == {}


def test_plan_accept_premium_unblocks(client) -> None:
    """accept_premium=True clears `blocked` and emits the feature's env knob."""
    resp = client.post(
        "/api/v1/runs/plan",
        json={
            "recipe": RECIPE,
            "features": {PREMIUM_FEATURE: PREMIUM_VALUE},
            "accept_premium": True,
        },
    )
    assert resp.status_code == 200
    feats = resp.json()["features"]
    assert feats["blocked"] == []
    assert feats["env"] == {"MO_REVIEW_PANEL": PREMIUM_VALUE}
    assert feats["cost_multiplier"] == 3.0


def test_plan_unknown_recipe_is_400(client) -> None:
    """AC4: an unknown recipe is a 400 with a non-empty detail — never a 500."""
    resp = client.post("/api/v1/runs/plan", json={"recipe": "no-such-recipe-xyz"})
    assert resp.status_code == 400
    assert resp.json().get("detail")
