"""The router's z-score must stay on the same scale as the UCB bonus.

Live state.db (2026-09-29), goal_loop/implementer/code-delivery: slice_mean
2.94, slice_std 0.003, every lane's z near -900. Two bugs compounded:

- the slice baseline divided its moments by the GROUP count, so the "mean" was
  a sum and the variance sum(x^2) - sum(x)^2 clamped to 0;
- ``_zscore`` subtracted that raw-score mean from an advantage that is already
  group-relative, then divided by a 1e-3 floor.

A 0.06 advantage gap became ~20 z-units, the UCB bonus (<~1.1) could never
reorder lanes, and the router locked onto a lane backed by one run.
"""
from __future__ import annotations

import datetime
import itertools
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import lane_router  # noqa: E402

_NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
_SEQ = itertools.count(1)
KNOBS = {"MO_LEARNING_HALFLIFE_DAYS": "0", "MO_LEARNING_DECAY_ALPHA": "1.0",
         "MO_LEARNING_TIEBREAK": "0", "MO_ROUTER_UCB_C": "0.5",
         "MO_ROUTER_CONTEXTUAL": "0", "MO_EQUIROUTER": "0",
         "MO_ENTROROUTER": "0", "MO_LEARNING_MIN_SAMPLES": "1"}


def _init_db(tmp_path) -> str:
    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    return db


def _ins(db, lane, reward):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO execution_traces (trace_id, agent_version_id, task_class, "
        "objective_domain, code_region, verifier_output, reward_g, cost_usd, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"t-{lane}-{next(_SEQ)}", lane, "tc", "code-delivery", "",
         '{"node_type":"implementer"}', reward, 1.0, "success", _NOW))
    con.commit()
    con.close()


def _recompute(db, monkeypatch):
    for k, v in KNOBS.items():
        monkeypatch.setenv(k, v)
    lane_router.recompute_advantages(since=0, db=db)


def _z(db, lane):
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT z_score_advantage FROM lane_domain_advantage "
        "WHERE agent_version_id=? AND task_class='tc'", (lane,)).fetchone()
    con.close()
    return row[0]


def test_slice_baseline_is_a_per_run_mean_and_variance(tmp_path, monkeypatch):
    db = _init_db(tmp_path)
    for lane, r in (("a", 1.0), ("a", 1.0), ("b", 0.0), ("b", 0.0)):
        _ins(db, lane, r)

    _recompute(db, monkeypatch)

    con = sqlite3.connect(db)
    mean, var = con.execute(
        "SELECT slice_mean, slice_var FROM lane_slice_baseline "
        "WHERE task_class='tc'").fetchone()
    con.close()
    assert (mean, var) == (0.5, 0.25)  # was (2.0, 0.0): a sum, and a clamped negative


def test_near_constant_slice_keeps_z_on_the_ucb_scale(tmp_path, monkeypatch):
    """Scores that barely vary (the live goal_loop shape) must not produce
    z-scores in the hundreds."""
    db = _init_db(tmp_path)
    for r in (2.95, 2.95, 2.95):
        _ins(db, "proven", r)
    _ins(db, "fresh", 2.94)

    _recompute(db, monkeypatch)

    assert abs(_z(db, "proven")) < 1.0 and abs(_z(db, "fresh")) < 1.0



# ── runs_count counts runs, not groups ─────────────────────────────────────


def _ins_region(db, lane, reward, region):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO execution_traces (trace_id, agent_version_id, task_class, "
        "objective_domain, code_region, verifier_output, reward_g, cost_usd, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"t-{lane}-{next(_SEQ)}", lane, "tc", "code-delivery", region,
         '{"node_type":"implementer"}', reward, 1.0, "success", _NOW))
    con.commit()
    con.close()


def test_region_rows_count_runs_and_clear_the_sample_floor(tmp_path, monkeypatch):
    """Each code region is exactly one group, so persisting the group count
    pinned every lane_region_advantage row at runs_count=1 — below the default
    floor of 3, which made the region routing level unreachable."""
    db = _init_db(tmp_path)
    for lane, r in (("a", 1.0),) * 3 + (("b", 0.0),) * 3:
        _ins_region(db, lane, r, "mini_ork/cli")
    _recompute(db, monkeypatch)
    monkeypatch.setenv("MO_LEARNING_MIN_SAMPLES", "3")

    con = sqlite3.connect(db)
    counts = dict(con.execute(
        "SELECT agent_version_id, runs_count FROM lane_region_advantage").fetchall())
    con.close()
    assert counts == {"a": 3, "b": 3}
    detail = lane_router.preferred_lane_detail(
        "tc", "implementer", "code-delivery", "mini_ork/cli", db=db)
    assert (detail["lane"], detail["source"]) == ("a", "region")


def test_exploration_can_still_pick_the_less_tried_lane(tmp_path, monkeypatch):
    """A 0.01 score gap on 3 runs vs 1 run is noise; the UCB bonus must be able
    to send the next pick to the lane with less evidence. Before the z-score
    fix the z gap dwarfed the bonus, and before the runs_count fix both lanes
    had n=1 so the bonus could not tell them apart."""
    db = _init_db(tmp_path)
    for r in (2.95, 2.95, 2.95):
        _ins(db, "proven", r)
    _ins(db, "fresh", 2.94)
    _recompute(db, monkeypatch)

    pick = lane_router.preferred_lane("tc", "implementer", "code-delivery", db=db)

    assert pick.split("|")[0] == "fresh"
