"""Unit tests: mini_ork.lane_router (bash parity halves removed; formerly vs lib/lane_router.sh).

Seed execution_traces, run the Python recompute, and assert
agent_performance_memory / lane_region_advantage / lane_domain_advantage
advantages + preferred_lane semantics. Halflife is disabled
(MO_LEARNING_HALFLIFE_DAYS=0) where wall-clock drift matters.

The frc-a5 delayed-penalty fold and the GRPO write-half refinements assert
the documented directional behaviours (penalty drop scoping, decay
halving, malformed-row skip, shrinkage, tie-break, EMA blend, recency).
"""
from __future__ import annotations

import datetime
import itertools
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import lane_router, trace_store  # noqa: E402

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


def _recompute_py(db, knobs=None):
    old = os.environ.copy()
    os.environ.update(knobs or DETERM)
    try:
        lane_router.recompute_advantages(db=db)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_recompute_advantage(seeded):
    _recompute_py(seeded)
    rows = {(r[0], r[1]): r for r in _apm(seeded)}
    # Both groups present for the seeded lanes.
    assert ("laneA", "code-fix") in rows
    assert ("laneB", "code-fix") in rows
    # Clear winner: laneA (all rewards 1.0) has a positive advantage,
    # laneB (all rewards 0.0) a negative one.
    assert rows[("laneA", "code-fix")][2] > 0
    assert rows[("laneB", "code-fix")][2] < 0
    # Counts: one GRPO group of 3 traces per lane.
    assert rows[("laneA", "code-fix")][3] == 1
    assert rows[("laneB", "code-fix")][3] == 1


def test_preferred_lane(seeded):
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
    assert py.split("|")[0] == "laneA"                    # winner


# ── frc-a5 delayed-penalty fold ──
#
#   (1-2) baseline: blamed + unblamed lanes get a regionA row;
#   (3-6) a fresh negative penalty drops ONLY the blamed lane's region advantage
#         (unblamed lane, other region, and global+domain slices all unchanged);
#   (7-8) an aged penalty (decay 0.5) removes ~half as much, with a smaller
#         incremental drop than the fresh one;
#   (9)   a malformed halflife=0.0 row is skipped, not silently invented.


def _init_db(tmp_path, name="fold.db"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = str(home / name)
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": base},
                   capture_output=True, text=True, check=True)
    return base


def _seed_region(db, region):
    """Insert 4 execution_traces in `region`: codex_lens wins 2/2, kimi_lens loses
    2/2 (objective_domain=code-delivery, task_class=code-fix, node_type=implementer)."""
    con = sqlite3.connect(db)
    con.executescript(f"""
    INSERT INTO execution_traces
      (trace_id, run_id, workflow_version_id, agent_version_id, task_class,
       prompt_version_hash, context_bundle_hash, tool_calls, files_read,
       files_written, verifier_output, reviewer_verdict, cost_usd, duration_ms,
       final_artifact_ref, status, process_reward, objective_domain, reward_g,
       reward_direction, reward_source, validity, code_region, created_at)
    VALUES
      ('t-cx-1-{region}', NULL,'wf','codex_lens','code-fix','p','ctx','[]','[]','[]',
       '{{"node_type":"implementer"}}','APPROVE',0.05,200,'a','success',0.95,
       'code-delivery',0.95,'higher_is_better','v@1','valid','{region}','2026-06-01T00:00:00.000Z'),
      ('t-cx-2-{region}', NULL,'wf','codex_lens','code-fix','p','ctx','[]','[]','[]',
       '{{"node_type":"implementer"}}','APPROVE',0.05,200,'a','success',0.95,
       'code-delivery',0.95,'higher_is_better','v@1','valid','{region}','2026-06-01T00:00:00.000Z'),
      ('t-km-1-{region}', NULL,'wf','kimi_lens','code-fix','p','ctx','[]','[]','[]',
       '{{"node_type":"implementer"}}','REJECT',0.01,200,'a','failure',0.05,
       'code-delivery',0.05,'higher_is_better','v@1','valid','{region}','2026-06-01T00:00:00.000Z'),
      ('t-km-2-{region}', NULL,'wf','kimi_lens','code-fix','p','ctx','[]','[]','[]',
       '{{"node_type":"implementer"}}','REJECT',0.01,200,'a','failure',0.05,
       'code-delivery',0.05,'higher_is_better','v@1','valid','{region}','2026-06-01T00:00:00.000Z');
    """)
    con.commit()
    con.close()


def _insert_defect(db, lane, region, penalty, hlf, age_days, tc="code-fix"):
    """Insert a defect_attributions row with ts = now(UTC) - age_days (so decay is
    reproducible). The ts is emitted as %Y-%m-%dT%H:%M:%S.%fZ (seconds + micros):
    the fold's ts-parser only accepts %H:%M:%S[.%f]."""
    ts = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO defect_attributions (found_run_id, blamed_run_id, lane, "
        "code_region, task_class, severity, penalty, decay_halflife_days, ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"rf-{region}-{lane}", f"rb-{region}-{lane}", lane, region, tc,
         "high", penalty, hlf, ts))
    con.commit()
    con.close()


def _radv(db, lane, region, tc="code-fix"):
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT relative_advantage FROM lane_region_advantage "
        "WHERE agent_version_id=? AND code_region=? AND task_class=?",
        (lane, region, tc)).fetchone()
    con.close()
    return None if row is None else round(row[0], 4)


def _dadv(db, lane, od="code-delivery", tc="code-fix"):
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT relative_advantage FROM lane_domain_advantage "
        "WHERE agent_version_id=? AND objective_domain=? AND task_class=?",
        (lane, od, tc)).fetchone()
    con.close()
    return None if row is None else round(row[0], 4)


def _gadv(db, lane, tc="code-fix"):
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT relative_advantage FROM agent_performance_memory "
        "WHERE agent_version_id=? AND task_class=?", (lane, tc)).fetchone()
    con.close()
    return None if row is None else round(row[0], 4)


def test_lr_fold_baseline_rows_exist(tmp_path):
    """After a no-defect recompute the blamed and unblamed lanes both have a
    regionA row."""
    db = _init_db(tmp_path)
    _seed_region(db, "regionA")
    _seed_region(db, "regionB")
    _recompute_py(db)
    assert _radv(db, "codex_lens", "regionA") is not None, "codex_lens regionA row missing"
    assert _radv(db, "kimi_lens", "regionA") is not None, "kimi_lens regionA row missing"


def test_lr_fold_fresh_penalty_drop_and_scope(tmp_path):
    """A fresh (age=0, halflife=30) -0.5 penalty on codex_lens regionA drops
    that lane's advantage, while the unblamed lane (kimi regionA), the same
    lanes in regionB, and the global + per-domain slices stay unchanged."""
    import shutil
    db = _init_db(tmp_path)
    _seed_region(db, "regionA")
    _seed_region(db, "regionB")
    # fresh copy per recompute → no EMA blending with prior runs
    d_base = db + ".base"
    shutil.copy(db, d_base)
    _recompute_py(d_base)
    base_cx_a = _radv(d_base, "codex_lens", "regionA")
    base_km_a = _radv(d_base, "kimi_lens", "regionA")
    base_cx_b = _radv(d_base, "codex_lens", "regionB")
    base_km_b = _radv(d_base, "kimi_lens", "regionB")
    base_cx_dom = _dadv(d_base, "codex_lens")
    base_km_dom = _dadv(d_base, "kimi_lens")
    base_cx_gl = _gadv(d_base, "codex_lens")
    base_km_gl = _gadv(d_base, "kimi_lens")

    d_fresh = db + ".fresh"
    shutil.copy(db, d_fresh)
    _insert_defect(d_fresh, "codex_lens", "regionA", -0.5, 30.0, 0.0)
    _recompute_py(d_fresh)

    after_cx_a = _radv(d_fresh, "codex_lens", "regionA")
    assert base_cx_a is not None and after_cx_a is not None
    # blamed lane drops in regionA
    assert after_cx_a < base_cx_a, f"codex regionA did not drop: {base_cx_a} -> {after_cx_a}"
    # unblamed lane (kimi) regionA unchanged
    assert _radv(d_fresh, "kimi_lens", "regionA") == base_km_a
    # no region leakage — same lanes in regionB unchanged
    assert _radv(d_fresh, "codex_lens", "regionB") == base_cx_b
    assert _radv(d_fresh, "kimi_lens", "regionB") == base_km_b
    # global + per-domain slices untouched by a region penalty
    assert _dadv(d_fresh, "codex_lens") == base_cx_dom
    assert _dadv(d_fresh, "kimi_lens") == base_km_dom
    assert _gadv(d_fresh, "codex_lens") == base_cx_gl
    assert _gadv(d_fresh, "kimi_lens") == base_km_gl


def test_lr_fold_aged_penalty_decay_and_ordering(tmp_path):
    """An aged penalty (age=30, halflife=30 → decay 0.5) contributes ~half a
    fresh one, with a smaller incremental drop than the fresh one."""
    import shutil
    db = _init_db(tmp_path)
    _seed_region(db, "regionA")
    _seed_region(db, "regionB")

    # baseline (fresh copy → no EMA blending)
    d_base = db + ".base"
    shutil.copy(db, d_base)
    _recompute_py(d_base)
    base = _radv(d_base, "codex_lens", "regionA")

    # fresh penalty only
    d_pre = db + ".pre"
    shutil.copy(db, d_pre)
    _insert_defect(d_pre, "codex_lens", "regionA", -0.5, 30.0, 0.0)
    _recompute_py(d_pre)
    pre = _radv(d_pre, "codex_lens", "regionA")

    # fresh + aged penalty
    d_aged = db + ".aged"
    shutil.copy(db, d_aged)
    _insert_defect(d_aged, "codex_lens", "regionA", -0.5, 30.0, 0.0)
    _insert_defect(d_aged, "codex_lens", "regionA", -0.5, 30.0, 30.0)
    _recompute_py(d_aged)
    after = _radv(d_aged, "codex_lens", "regionA")

    assert base is not None and pre is not None and after is not None
    # aged penalty halves the contribution → ~0.25 extra drop vs fresh-only
    assert abs((pre - after) - 0.25) < 5e-4, f"base={base} pre={pre} after={after}"
    # fresh drop (base→pre) larger than aged incremental drop (pre→after)
    assert abs(pre - after) < abs(base - pre), f"base={base} pre={pre} after={after}"


def test_lr_fold_malformed_halflife_skipped(tmp_path):
    """A malformed defect row (halflife=0.0) is skipped — not silently
    invented — so it neither crashes the recompute nor changes the lane's
    advantage."""
    import shutil
    db = _init_db(tmp_path)
    _seed_region(db, "regionA")
    _seed_region(db, "regionB")
    d_base = db + ".base"
    shutil.copy(db, d_base)
    _recompute_py(d_base)
    base_km_a = _radv(d_base, "kimi_lens", "regionA")

    d_m = db + ".malformed"
    shutil.copy(db, d_m)
    _insert_defect(d_m, "kimi_lens", "regionA", -0.9, 0.0, 0.0)
    _recompute_py(d_m)

    assert base_km_a is not None
    # malformed halflife=0.0 skipped — kimi_lens regionA unchanged
    assert _radv(d_m, "kimi_lens", "regionA") == base_km_a


# ── GRPO write-half refinements ──
#
#   (1) n-aware shrinkage — SHRINK_K=5 pulls a small-n (n=1) advantage below its
#       SHRINK_K=0 raw value;
#   (2) sigma-zero cost tie-break — on a flat-score group TIEBREAK=1 favours the
#       cheaper lane (adv>0) over the dearer (adv<0); TIEBREAK=0 leaves it at 0;
#   (3) EMA decay blend — a second recompute with DECAY_ALPHA=0.30 blends toward
#       the stored prior (0.30*batch2 + 0.70*batch1 = +0.20) instead of overwriting;
#   (4) recency weighting — HALFLIFE=14 down-weights an old opposing trace, lifting
#       the advantage above the HALFLIFE=0 (flat) value.

_NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
_OLD = (datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
_TRACE_SEQ = itertools.count(1)  # monotonic trace_id counter, unique across the session


def _ins_trace(db, lane, rg, cost, created_at, region=""):
    """Insert one success trace (task_class='tc', objective_domain='code-delivery',
    node_type='implementer')."""
    n = next(_TRACE_SEQ)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO execution_traces (trace_id, agent_version_id, task_class, "
        "objective_domain, code_region, verifier_output, reward_g, cost_usd, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"t-{lane}-{n}", lane, "tc", "code-delivery", region,
         '{"node_type":"implementer"}', rg, cost, "success", created_at))
    con.commit()
    con.close()


def _recompute_py_knobs(db, knobs):
    old = os.environ.copy()
    os.environ.update(knobs)
    try:
        lane_router.recompute_advantages(since=0, db=db)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_lr_ref_shrinkage(tmp_path):
    """SHRINK_K=5 pulls a small-n advantage below its SHRINK_K=0 raw value."""
    db = _init_db(tmp_path, "shrink.db")
    _ins_trace(db, "laneA", 1.0, 1, _NOW)
    _ins_trace(db, "laneB", 0.0, 1, _NOW)
    common = {"MO_LEARNING_DECAY_ALPHA": "1.0", "MO_LEARNING_HALFLIFE_DAYS": "0",
              "MO_LEARNING_TIEBREAK": "0"}

    import shutil
    def one(shrink_k):
        d = f"{db}.s{shrink_k}"
        shutil.copy(db, d)
        _recompute_py_knobs(d, {**common, "MO_LEARNING_SHRINKAGE_K": str(shrink_k)})
        return _gadv(d, "laneA", tc="tc")

    shrunk = one(5)
    raw = one(0)
    assert shrunk is not None and raw is not None
    assert 0 < shrunk < raw and raw > 0.4, f"shrunk={shrunk} raw={raw}"


def test_lr_ref_tiebreak(tmp_path):
    """On a flat-score group TIEBREAK=1 favours the cheaper lane over the
    dearer; TIEBREAK=0 leaves it at 0."""
    db = _init_db(tmp_path, "tie.db")
    _ins_trace(db, "laneC", 0.5, 1, _NOW)
    _ins_trace(db, "laneD", 0.5, 100, _NOW)
    common = {"MO_LEARNING_DECAY_ALPHA": "1.0", "MO_LEARNING_HALFLIFE_DAYS": "0",
              "MO_LEARNING_SHRINKAGE_K": "0"}

    import shutil
    def one(tb):
        d = f"{db}.t{tb}"
        shutil.copy(db, d)
        _recompute_py_knobs(d, {**common, "MO_LEARNING_TIEBREAK": str(tb)})
        return d

    d1 = one(1)
    c, d_ = _gadv(d1, "laneC", tc="tc"), _gadv(d1, "laneD", tc="tc")
    d0 = one(0)
    c0 = _gadv(d0, "laneC", tc="tc")

    assert c is not None and d_ is not None and c0 is not None
    assert c > d_, f"TIEBREAK=1 cheaper C={c} !> dearer D={d_}"
    assert -0.0001 <= c0 <= 0.0001, f"TIEBREAK=0 flat group C={c0} != ~0"


def test_lr_ref_ema_blend(tmp_path):
    """A second recompute with DECAY_ALPHA=0.30 blends toward the stored
    prior (→ ~+0.20) rather than overwriting."""
    db = _init_db(tmp_path, "ema.db")
    pass1 = {"MO_LEARNING_SHRINKAGE_K": "0", "MO_LEARNING_HALFLIFE_DAYS": "0",
             "MO_LEARNING_TIEBREAK": "0", "MO_LEARNING_DECAY_ALPHA": "1.0"}
    pass2 = {**pass1, "MO_LEARNING_DECAY_ALPHA": "0.30"}
    _ins_trace(db, "laneA", 1.0, 1, _NOW)
    _ins_trace(db, "laneB", 0.0, 1, _NOW)
    _recompute_py_knobs(db, pass1)
    con = sqlite3.connect(db)   # drop only traces; keep the stored prior
    con.execute("DELETE FROM execution_traces")
    con.commit()
    con.close()
    _ins_trace(db, "laneA", 0.0, 1, _NOW)
    _ins_trace(db, "laneB", 1.0, 1, _NOW)
    _recompute_py_knobs(db, pass2)

    blended = _gadv(db, "laneA", tc="tc")
    assert blended is not None
    assert 0.1 < blended < 0.3, f"EMA blended={blended} (expect ~0.20)"


def test_lr_ref_recency(tmp_path):
    """HALFLIFE=14 down-weights an old opposing trace, lifting laneA's
    advantage above the HALFLIFE=0 (flat) value."""
    common = {"MO_LEARNING_SHRINKAGE_K": "0", "MO_LEARNING_DECAY_ALPHA": "1.0",
              "MO_LEARNING_TIEBREAK": "0"}

    def one(hl):
        db = _init_db(tmp_path, f"rec{hl}.db")
        _ins_trace(db, "laneA", 1.0, 1, _NOW)
        _ins_trace(db, "laneA", 0.0, 1, _OLD)
        _ins_trace(db, "laneB", 0.5, 1, _NOW)
        _recompute_py_knobs(db, {**common, "MO_LEARNING_HALFLIFE_DAYS": str(hl)})
        return _gadv(db, "laneA", tc="tc")

    rec = one(14)
    flat = one(0)
    assert rec is not None and flat is not None
    assert rec > flat, f"recency h14={rec} !> h0={flat}"
