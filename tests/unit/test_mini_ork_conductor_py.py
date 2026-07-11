"""Parity gate: mini_ork.ported.mini_ork_conductor vs bin/mini-ork-conductor.

Seed a ready epic + a topology_win_rate, run --once --dry-run through the LIVE
bash conductor and the port on separate DBs, and compare the logged
conductor_decisions row (decided_at excluded). Plus a unit check of the
predicted-score / budget-bias branches.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_conductor as cond  # noqa: E402

BIN = REPO / "bin" / "mini-ork-conductor"


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


def test_decision_logged_parity(tmp_path):
    hb, db_b = _seed(tmp_path, "b")
    _, db_p = _seed(tmp_path, "p")
    subprocess.run(["bash", str(BIN), "--once", "--dry-run"], capture_output=True, text=True,
                   env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": hb, "MINI_ORK_DB": db_b})
    cond.main(["--once", "--dry-run"], db=db_p, root=str(REPO))
    q = f"SELECT {_DEC_COLS} FROM conductor_decisions"
    rb, rp = _sql(db_b, q), _sql(db_p, q)
    assert rb == rp and rb  # identical decision persisted
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


def test_empty_queue(tmp_path):
    hb, db_b = _seed(tmp_path, "e")
    _sql(db_b, "UPDATE epics SET status='done' WHERE id='e1';")   # nothing ready
    rb = subprocess.run(["bash", str(BIN), "--once"], capture_output=True, text=True,
                        env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": hb,
                             "MINI_ORK_DB": db_b}).returncode
    rp = cond.main(["--once"], db=db_b, root=str(REPO))
    assert rb == rp == 2   # empty queue → _tick returns 2, sourced-lib errexit aborts
