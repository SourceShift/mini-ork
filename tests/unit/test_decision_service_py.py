"""Unit tests: mini_ork.steering.decision_service.decide (bash parity halves removed; formerly vs lib/decision_service.sh).

EPSILON=0 disables exploration so behavior is deterministic. Two paths:
cold-start (no traces -> agents.yaml default lane) and learned (seeded winner
clears the sample floor -> learned route). No mocking, no hardcoded lane names
on the cold-start path (the expected default is read from agents.yaml itself).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import lane_router, trace_store  # noqa: E402
from mini_ork.dispatch import calibration as cal  # noqa: E402
from mini_ork.steering import decision_service as ds


@pytest.fixture
def env_db(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    for k, v in {"MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home),
                 "MINI_ORK_DB": dbp, "MO_STORE_DB": dbp, "EPSILON": "0",
                 "MO_LEARNING_MIN_SAMPLES": "1",
                 "MO_LEARNING_HALFLIFE_DAYS": "0"}.items():
        monkeypatch.setenv(k, v)
    return dbp


def test_cold_start(env_db):
    p = ds.decide("implementer", "code-fix", "code-delivery", db=env_db)
    # Cold start: agents.yaml default lane, no sample, coalition ok.
    expected_default = ds.default_lane("implementer")
    assert expected_default, "agents.yaml must configure an implementer lane"
    assert p["route"] == expected_default
    assert p["coalition_ok"] is True
    assert p["sample_size"] == 0
    assert "recursion_hint" in p


def test_learned_route(env_db):
    # Seed a clear winner (laneA > laneB) then recompute advantages.
    def seed(lane, rv, n=3):
        for _ in range(n):
            trace_store.trace_write(
                {"task_class": "code-fix", "status": "success",
                 "agent_version_id": lane, "objective_domain": "code-delivery",
                 "verifier_output": {"node_type": "implementer"},
                 "reward_value": rv, "reward_anchor": 0.5,
                 "reward_direction": "higher_is_better"}, db=env_db)
    seed("laneA", 1.0)
    seed("laneB", 0.0)
    lane_router.recompute_advantages(db=env_db)
    p = ds.decide("implementer", "code-fix", "code-delivery", db=env_db)
    assert p["route"] == "laneA"
    assert p["sample_size"] > 0
    assert "reward_estimate" in p


def test_seeded_exploration_is_deterministic(env_db, monkeypatch):
    # With EPSILON=1 + fixed SEED, exploration must pick the same lane on
    # repeated calls (stable sorted-candidates + seeded RNG discipline).
    monkeypatch.setenv("EPSILON", "1.0")
    monkeypatch.setenv("SEED", "42")
    r1 = ds._explore_route("sonnet", 1.0, "42", ds.resolve_agents_yaml())
    r2 = ds._explore_route("sonnet", 1.0, "42", ds.resolve_agents_yaml())
    assert r1 == r2 and r1 != "sonnet"


def test_route_provenance_labels_the_decision(env_db, monkeypatch):
    """Every route carries WHY it was chosen. Without this the three sources are
    indistinguishable after the fact, so no outcome can be credited to (or debited
    from) the decision that produced it — which is the signal EquiRouter ranks on."""
    # cold start -> the agents.yaml default, never an invented lane
    p = ds.decide("implementer", "code-fix", "code-delivery", db=env_db)
    assert p["route_source"] == "default"
    assert p["route_explore"] is False
    assert p["route_score"] is None

    def seed(lane, rv, n=3):
        for _ in range(n):
            trace_store.trace_write(
                {"task_class": "code-fix", "status": "success",
                 "agent_version_id": lane, "objective_domain": "code-delivery",
                 "verifier_output": {"node_type": "implementer"},
                 "reward_value": rv, "reward_anchor": 0.5,
                 "reward_direction": "higher_is_better"}, db=env_db)
    seed("laneA", 1.0)
    seed("laneB", 0.0)
    lane_router.recompute_advantages(db=env_db)

    # learned -> the route is attributable to the advantage row that won it
    p = ds.decide("implementer", "code-fix", "code-delivery", db=env_db)
    assert p["route"] == "laneA"
    assert p["route_source"] == "learned"
    assert p["route_explore"] is False
    assert p["route_score"] is not None

    # explore -> an epsilon-greedy swap is labelled as one, not as a learn
    monkeypatch.setenv("EPSILON", "1.0")
    monkeypatch.setenv("SEED", "42")
    q = ds.decide("implementer", "code-fix", "code-delivery", db=env_db)
    assert q["route"] != "laneA"
    assert q["route_source"] == "explore"
    assert q["route_explore"] is True
    # the score still reports the estimate that was overridden, not the swap
    assert q["route_score"] == p["route_score"]


def test_calibrated_escalation_labels_the_route(env_db, monkeypatch):
    """UCCI turns the margin into a predicted error and escalates past it.

    The learned lane wins the router's ranking, but the calibration map says a
    win this narrow has historically meant an error for this task class, so the
    decision escalates to the frontier lane. The point of the label is that the
    escalation is distinguishable after the fact from a learned pick or an
    agents.yaml default — otherwise the escalation's own outcome is unattributable
    and the threshold can never be re-derived from what it predicted.
    """
    monkeypatch.setenv("MO_UCCI_CACHE_TTL", "0")
    monkeypatch.delenv("MO_UCCI", raising=False)
    monkeypatch.delenv("MO_UCCI_TARGET_ERROR", raising=False)
    monkeypatch.setenv("MO_FRONTIER_LANE", "opus_lens")
    cal.clear_cache()

    def seed(lane, rv, n=3, margin=None, status="success"):
        for _ in range(n):
            payload = {"task_class": "code-fix", "status": status,
                       "agent_version_id": lane, "objective_domain": "code-delivery",
                       "verifier_output": {"node_type": "implementer"},
                       "reward_value": rv, "reward_anchor": 0.5,
                       "reward_direction": "higher_is_better"}
            if margin is not None:
                payload["route_margin"] = margin
            trace_store.trace_write(payload, db=env_db)

    # laneA wins the ranking with laneB as runner-up, so the margin is thin.
    seed("laneA", 1.0)
    seed("laneB", 0.0)
    # Other lanes' narrow losses fill the pooled map (laneA itself has no margin
    # rows, so the per-lane fit abstains and the pooled slice answers).
    for lane in ("laneC", "laneD", "laneE"):
        seed(lane, 0.0, n=4, margin=0.05, status="failure")
    lane_router.recompute_advantages(db=env_db)

    p = ds.decide("implementer", "code-fix", "code-delivery", db=env_db)
    assert p["route"] == "opus_lens"
    assert p["route_source"] == "calibrated_escalation"
    assert p["route_explore"] is False
    assert p["predicted_error"] is not None and p["predicted_error"] > 0.15
    # The escalation replaces the lane, not the record of the comparison it
    # replaced — the winning estimate stays visible for the next refit.
    assert p["route_score"] is not None


def test_opt_out_restores_uncalibrated_routing(env_db, monkeypatch):
    """MO_UCCI=0 leaves the learned lane in place: the margin is still recorded
    (so the map keeps accumulating rows) but nothing escalates on it."""
    monkeypatch.setenv("MO_UCCI_CACHE_TTL", "0")
    monkeypatch.setenv("MO_UCCI", "0")
    monkeypatch.setenv("MO_FRONTIER_LANE", "opus_lens")
    cal.clear_cache()

    def seed(lane, rv, n=3, margin=None, status="success"):
        for _ in range(n):
            payload = {"task_class": "code-fix", "status": status,
                       "agent_version_id": lane, "objective_domain": "code-delivery",
                       "verifier_output": {"node_type": "implementer"},
                       "reward_value": rv, "reward_anchor": 0.5,
                       "reward_direction": "higher_is_better"}
            if margin is not None:
                payload["route_margin"] = margin
            trace_store.trace_write(payload, db=env_db)

    seed("laneA", 1.0)
    seed("laneB", 0.0)
    for lane in ("laneC", "laneD", "laneE"):
        seed(lane, 0.0, n=4, margin=0.05, status="failure")
    lane_router.recompute_advantages(db=env_db)

    p = ds.decide("implementer", "code-fix", "code-delivery", db=env_db)
    assert p["route"] == "laneA"
    assert p["route_source"] == "learned"
    assert p["predicted_error"] is None
