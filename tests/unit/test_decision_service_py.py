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
