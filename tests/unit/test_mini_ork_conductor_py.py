"""Unit tests: mini_ork.orchestration.conductor (bash parity halves removed; formerly vs bin/mini-ork-conductor).

Seed a ready epic + a topology_win_rate, run --once --dry-run through the
port and assert the logged conductor_decisions row (decided_at excluded).
Plus unit checks of the predicted-score / budget-bias / adaptive-gain
branches.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.orchestration import conductor as cond


def _sql(db, stmt):
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


def _seed(tmp_path, name):
    home = tmp_path / name / ".mini-ork"; home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    _sql(db, "INSERT INTO epics (id,title,status,kickoff_path) VALUES ('e1','E1','not started','k.md');")
    _sql(db, "INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,created_at,updated_at) "
             "VALUES ('r1','code_fix','latest','k.md','classified',strftime('%s','now'),strftime('%s','now'));")
    _sql(db, "INSERT INTO topology_win_rates (topology_id,workflow_name,task_class,wins,losses,ties,"
             "win_rate,sample_size,avg_cost_usd) VALUES ('topo-1','code-fix','code_fix',8,2,0,0.8,5,1.0);")
    return str(home), db


_DEC_COLS = ("epic_id,task_class,chosen_topology,chosen_recipe,chosen_lane_hints,"
             "predicted_score,budget_pct_used,rationale,outcome")


def test_decision_logged(tmp_path):
    _, db_p = _seed(tmp_path, "p")
    cond.main(["--once", "--dry-run"], db=db_p, root=str(REPO))
    rp = _sql(db_p, f"SELECT {_DEC_COLS} FROM conductor_decisions")
    assert rp  # a decision was persisted
    # sanity: chose topo-1 / code-fix, predicted 0.8 (no lane advantage available)
    assert "topo-1" in rp and "code-fix" in rp and "0.8" in rp


def test_decide_predicted_and_budget_bias(tmp_path):
    _, db = _seed(tmp_path, "u")
    # add a cheaper topology; under >70% budget the cheap one wins
    _sql(db, "INSERT INTO topology_win_rates (topology_id,workflow_name,task_class,wins,losses,ties,"
             "win_rate,sample_size,avg_cost_usd) VALUES ('topo-cheap','cheapo','code_fix',5,5,0,0.5,4,0.10);")
    hi = cond.decide_for_epic(db, "e1", spent=0.0, cap=50.0, plast_budget=5)
    assert hi["topology"]["topology_id"] == "topo-1"        # best win_rate when budget ok
    assert hi["predicted_score"] == 0.8
    lo = cond.decide_for_epic(db, "e1", spent=40.0, cap=50.0, plast_budget=5)  # 80% > 70%
    assert lo["topology"]["topology_id"] == "topo-cheap"    # cheapest under pressure
    assert "budget pressure" in lo["topology_rationale"]


def _seed_lane_adv(db, task_class="code_fix"):
    """Give one lane a positive relative_advantage so lane_adv_sum > 0 and the
    gain coefficient actually moves predicted_score."""
    _sql(db, "INSERT INTO agent_performance_memory "
             "(agent_version_id, task_class, role, model, runs_count, relative_advantage) "
             f"VALUES ('impl-v1','{task_class}','implementer','implementer',5,1.0);")


def _seed_realized(db, scores):
    for i, s in enumerate(scores):
        _sql(db, "INSERT INTO conductor_decisions "
                 "(decided_at, epic_id, task_class, predicted_score, budget_pct_used, "
                 "outcome, realized_score) "
                 f"VALUES ({1000+i}, 'e-old-{i}', 'code_fix', 0.5, 0.0, "
                 f"'{'success' if s>=0.5 else 'failure'}', {s});")


def test_adaptive_gain_cold_defaults_to_030(tmp_path):
    """Cold table (no resolved decisions) → gain stays 0.3, matching the legacy
    fixed constant. predicted = 0.8 * (1 + 1.0*0.3) = 1.04 → clamped to 1.0."""
    _, db = _seed(tmp_path, "gc")
    _seed_lane_adv(db)
    dec = cond.decide_for_epic(db, "e1", spent=0.0, cap=50.0, plast_budget=5)
    assert dec["lane_hints"].get("implementer") == "impl-v1"
    assert dec["predicted_score"] == 1.0  # 0.8*(1+0.3) clamped


def test_adaptive_gain_optout_matches_cold(tmp_path, monkeypatch):
    """MO_CONDUCTOR_ADAPTIVE_GAIN=0 forces the fixed 0.3 even with realized rows."""
    _, db = _seed(tmp_path, "go")
    _seed_lane_adv(db)
    _seed_realized(db, [1.0, 1.0, 1.0, 1.0])   # strong track record present
    monkeypatch.setenv("MO_CONDUCTOR_ADAPTIVE_GAIN", "0")
    dec = cond.decide_for_epic(db, "e1", spent=0.0, cap=50.0, plast_budget=5)
    assert dec["predicted_score"] == 1.0  # gain forced to 0.3 → 0.8*1.3 clamped


def test_adaptive_gain_poor_record_damps_predicted(tmp_path, monkeypatch):
    """A poor realized track record (ema<0.5) shrinks the gain below 0.3 so the
    lane-advantage boost is damped and predicted stays below the cold value."""
    monkeypatch.delenv("MO_CONDUCTOR_ADAPTIVE_GAIN", raising=False)
    _, db_p = _seed(tmp_path, "gpp")
    _seed_lane_adv(db_p)
    _seed_realized(db_p, [0.0, 0.0, 0.0, 0.0])   # all failures → ema=0 → gain floor 0.1
    dec = cond.decide_for_epic(db_p, "e1", spent=0.0, cap=50.0, plast_budget=5)
    # gain = clamp(0.3*2*ema=0, 0.1, 0.6) = 0.1 → 0.8*(1+1.0*0.1) = 0.88
    assert abs(dec["predicted_score"] - 0.88) < 1e-6


def test_empty_queue(tmp_path):
    _, db_b = _seed(tmp_path, "e")
    _sql(db_b, "UPDATE epics SET status='done' WHERE id='e1';")   # nothing ready
    rp = cond.main(["--once"], db=db_b, root=str(REPO))
    assert rp == 2   # empty queue → _tick returns 2
