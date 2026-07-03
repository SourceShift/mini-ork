"""Parity gate: mini_ork.lane_router vs lib/lane_router.sh (GRPO advantage).

Seed execution_traces, copy the DB, run the LIVE bash recompute on one copy and
the Python recompute on the other, and assert agent_performance_memory advantages
+ preferred_lane match. Halflife is disabled (MO_LEARNING_HALFLIFE_DAYS=0) so the
recency weight can't drift between the two runs' wall-clock.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import lane_router, trace_store  # noqa: E402

LR_SH = REPO / "lib" / "lane_router.sh"
DETERM = {"MO_LEARNING_HALFLIFE_DAYS": "0"}


@pytest.fixture
def seeded(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    base = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": base},
                   capture_output=True, text=True, check=True)
    # Two groups, two lanes each, clear winners.
    def seed(lane, od, tc, node, rv, ra, n):
        for _ in range(n):
            trace_store.trace_write(
                {"task_class": tc, "status": "success", "agent_version_id": lane,
                 "objective_domain": od, "verifier_output": {"node_type": node},
                 "reward_value": rv, "reward_anchor": ra, "reward_direction": "higher_is_better"},
                db=base)
    seed("laneA", "code-delivery", "code-fix", "implementer", 1.0, 0.5, 3)   # +1
    seed("laneB", "code-delivery", "code-fix", "implementer", 0.0, 0.5, 3)   # -1
    seed("laneA", "book-gen", "chapter", "writer", 0.9, 0.6, 3)
    seed("laneC", "book-gen", "chapter", "writer", 0.2, 0.6, 3)
    return base


def _apm(db):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT agent_version_id, task_class, round(relative_advantage,4), "
        "runs_count, success_count FROM agent_performance_memory "
        "ORDER BY agent_version_id, task_class").fetchall()
    con.close()
    return rows


def test_recompute_advantage_parity(seeded):
    db_bash = seeded + ".bash"
    db_py = seeded + ".py"
    shutil.copy(seeded, db_bash)
    shutil.copy(seeded, db_py)
    subprocess.run(
        ["bash", "-c", f'. "{LR_SH}" && lane_router_recompute_advantages'],
        env={**os.environ, **DETERM, "MINI_ORK_DB": db_bash, "MO_STORE_DB": db_bash},
        capture_output=True, text=True, check=True)
    old = os.environ.copy()
    os.environ.update(DETERM)
    try:
        lane_router.recompute_advantages(db=db_py)
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert _apm(db_bash) == _apm(db_py), f"\nbash={_apm(db_bash)}\npy  ={_apm(db_py)}"


def test_preferred_lane_parity(seeded):
    # min_samples=1: with one group per lane, runs_count=1 must still clear the
    # floor so we exercise the actual pick (not the below-floor empty path).
    env = {**DETERM, "MO_LEARNING_MIN_SAMPLES": "1"}
    os.environ.update(env)
    try:
        lane_router.recompute_advantages(db=seeded)
        py = lane_router.preferred_lane("code-fix", "implementer", "code-delivery", db=seeded)
    finally:
        for k in env:
            os.environ.pop(k, None)
    bash = subprocess.run(
        ["bash", "-c", f'. "{LR_SH}" && lane_router_preferred_lane code-fix implementer code-delivery'],
        env={**os.environ, **env, "MINI_ORK_DB": seeded, "MO_STORE_DB": seeded},
        capture_output=True, text=True).stdout.strip()
    assert py.split("|")[0] == "laneA"                    # winner
    assert bash.split("|")[0] == py.split("|")[0]         # bash == python
