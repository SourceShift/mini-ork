"""Parity gate: mini_ork.lane_router vs lib/lane_router.sh (GRPO advantage).

Seed execution_traces, copy the DB, run the LIVE bash recompute on one copy and
the Python recompute on the other, and assert agent_performance_memory advantages
+ preferred_lane match. Halflife is disabled (MO_LEARNING_HALFLIFE_DAYS=0) so the
recency weight can't drift between the two runs' wall-clock.

This gate also subsumes the retired tests/unit/test_lane_router.sh (9 assertions
for the frc-a5 delayed-penalty fold). See the "frc-a5 delayed-penalty fold parity"
section at the bottom for the per-group provenance; the .sh was a dead fixture at
retirement (its hand-picked migration subset predated migration 0009's
schema_migrations bookkeeping, so it SKIPped all 9 assertions), and these cases
revive its behaviours as genuinely-executing live-bash parity cases.
"""
from __future__ import annotations

import datetime
import itertools
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


# ── frc-a5 delayed-penalty fold parity (subsumes retired test_lane_router.sh) ──
#
# Provenance: tests/unit/test_lane_router.sh drove lane_router_recompute_advantages
# over a hand-picked 7-migration subset with 9 assertions across 4 phases —
#   (1-2) baseline: blamed + unblamed lanes get a regionA row;
#   (3-6) a fresh negative penalty drops ONLY the blamed lane's region advantage
#         (unblamed lane, other region, and global+domain slices all unchanged);
#   (7-8) an aged penalty (decay 0.5) removes ~half as much, with a smaller
#         incremental drop than the fresh one;
#   (9)   a malformed halflife=0.0 row is skipped, not silently invented.
# That migration subset rotted: migration 0009 now records itself into
# schema_migrations (added after the fixture was written) which the subset never
# creates, so the .sh aborted at setup with `no such table: schema_migrations` and
# SKIPped all 9 assertions (0 OK / 1 SKIP, vacuously green — `2>/dev/null` masked
# it). The cases below revive all 9 behaviours over the canonical db/init.sh schema
# (immune to subset drift): they seed the SAME execution_traces + defect_attributions,
# run the LIVE bash recompute and the port on identical DB copies, and assert both
# the .sh's behaviours (on the bash side) and bash==port across all three advantage
# tables. relative_advantage parity uses a 5e-4 tolerance — the fold's decay reads
# each side's independent utcnow(), a sub-second skew of ~1e-6 in advantage, well
# under the 5e-4 the .sh itself tolerated (its lines 309/319).


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
    2/2 (objective_domain=code-delivery, task_class=code-fix, node_type=implementer).
    Mirrors the retired .sh's _seed_regionA/_seed_regionB_traces exactly."""
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
    reproducible), mirroring the retired .sh's _insert_defect_attr. NOTE the ts is
    emitted as %Y-%m-%dT%H:%M:%S.%fZ (seconds + micros): the lib's ts-parser
    (lib/lane_router.sh ~L385) only accepts %H:%M:%S[.%f]. The .sh's own helper
    dropped %S (Python %f is micros, not SQLite's SS.SSS), so its ts was
    unparseable and its rows would have been silently skipped — a latent bug its
    dead-at-setup state masked; we emit the parseable form the fold actually reads."""
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


def _derive(db, suffix, defects):
    """Copy the seeded traces DB and add defect rows. defects: list of
    (lane, region, penalty, halflife, age_days)."""
    d = f"{db}.{suffix}"
    shutil.copy(db, d)
    for lane, region, pen, hlf, age in defects:
        _insert_defect(d, lane, region, pen, hlf, age)
    return d


def _recompute_bash(db):
    subprocess.run(
        ["bash", "-c", f'. "{LR_SH}" && lane_router_recompute_advantages'],
        env={**os.environ, **DETERM, "MINI_ORK_DB": db, "MO_STORE_DB": db},
        capture_output=True, text=True, check=True)


def _recompute_py(db):
    old = os.environ.copy()
    os.environ.update(DETERM)
    try:
        lane_router.recompute_advantages(db=db)
    finally:
        os.environ.clear()
        os.environ.update(old)


def _recompute_pair(db):
    """Copy `db`, run the LIVE bash recompute on one copy and the port on the
    other, and return (db_bash, db_py)."""
    db_bash, db_py = db + ".bash", db + ".py"
    shutil.copy(db, db_bash)
    shutil.copy(db, db_py)
    _recompute_bash(db_bash)
    _recompute_py(db_py)
    return db_bash, db_py


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


_ADV_TABLES = {
    "lane_region_advantage":
        ["agent_version_id", "task_class", "node_type", "objective_domain", "code_region"],
    "lane_domain_advantage":
        ["agent_version_id", "task_class", "node_type", "objective_domain"],
    "agent_performance_memory":
        ["agent_version_id", "task_class"],
}


def _assert_all_tables_parity(db_bash, db_py, tol=5e-4):
    """bash == port across all three advantage tables. relative_advantage within
    `tol` (absorbs the sub-second utcnow() decay skew); integer count columns
    exact; same key set."""
    for table, keys in _ADV_TABLES.items():
        cols = keys + ["relative_advantage", "runs_count", "success_count"]
        sel = f"SELECT {', '.join(cols)} FROM {table}"
        nk = len(keys)
        cb = sqlite3.connect(db_bash)
        rows_b = cb.execute(sel).fetchall()
        cb.close()
        cp = sqlite3.connect(db_py)
        rows_p = cp.execute(sel).fetchall()
        cp.close()
        b = {tuple(r[:nk]): r[nk:] for r in rows_b}
        p = {tuple(r[:nk]): r[nk:] for r in rows_p}
        assert set(b) == set(p), f"{table}: key sets differ\nbash={set(b)}\npy  ={set(p)}"
        for k in b:
            radv_b, runs_b, succ_b = b[k]
            radv_p, runs_p, succ_p = p[k]
            assert abs(radv_b - radv_p) < tol, f"{table}{k}: rel_adv bash={radv_b} py={radv_p}"
            assert (runs_b, succ_b) == (runs_p, succ_p), \
                f"{table}{k}: counts bash=({runs_b},{succ_b}) py=({runs_p},{succ_p})"


def test_lr_fold_baseline_rows_exist_parity(tmp_path):
    """Subsumes .sh groups 1-2: after a no-defect recompute the blamed and
    unblamed lanes both have a regionA row (asserted on the LIVE bash side); the
    port agrees across all three advantage tables."""
    db = _init_db(tmp_path)
    _seed_region(db, "regionA")
    _seed_region(db, "regionB")
    db_bash, db_py = _recompute_pair(db)
    assert _radv(db_bash, "codex_lens", "regionA") is not None, "codex_lens regionA row missing"
    assert _radv(db_bash, "kimi_lens", "regionA") is not None, "kimi_lens regionA row missing"
    _assert_all_tables_parity(db_bash, db_py)


def test_lr_fold_fresh_penalty_drop_and_scope_parity(tmp_path):
    """Subsumes .sh groups 3-6: a fresh (age=0, halflife=30) -0.5 penalty on
    codex_lens regionA drops that lane's advantage, while the unblamed lane (kimi
    regionA), the same lanes in regionB, and the global + per-domain slices stay
    unchanged (asserted on the LIVE bash side); the port agrees across all three
    tables."""
    db = _init_db(tmp_path)
    _seed_region(db, "regionA")
    _seed_region(db, "regionB")
    base_bash, _ = _recompute_pair(db)
    base_cx_a = _radv(base_bash, "codex_lens", "regionA")
    base_km_a = _radv(base_bash, "kimi_lens", "regionA")
    base_cx_b = _radv(base_bash, "codex_lens", "regionB")
    base_km_b = _radv(base_bash, "kimi_lens", "regionB")
    base_cx_dom = _dadv(base_bash, "codex_lens")
    base_km_dom = _dadv(base_bash, "kimi_lens")
    base_cx_gl = _gadv(base_bash, "codex_lens")
    base_km_gl = _gadv(base_bash, "kimi_lens")

    d = _derive(db, "fresh", [("codex_lens", "regionA", -0.5, 30.0, 0.0)])
    db_bash, db_py = _recompute_pair(d)

    after_cx_a = _radv(db_bash, "codex_lens", "regionA")
    assert base_cx_a is not None and after_cx_a is not None
    # sh3: blamed lane drops in regionA
    assert after_cx_a < base_cx_a, f"codex regionA did not drop: {base_cx_a} -> {after_cx_a}"
    # sh4: unblamed lane (kimi) regionA unchanged
    assert _radv(db_bash, "kimi_lens", "regionA") == base_km_a
    # sh5: no region leakage — same lanes in regionB unchanged
    assert _radv(db_bash, "codex_lens", "regionB") == base_cx_b
    assert _radv(db_bash, "kimi_lens", "regionB") == base_km_b
    # sh6: global + per-domain slices untouched by a region penalty
    assert _dadv(db_bash, "codex_lens") == base_cx_dom
    assert _dadv(db_bash, "kimi_lens") == base_km_dom
    assert _gadv(db_bash, "codex_lens") == base_cx_gl
    assert _gadv(db_bash, "kimi_lens") == base_km_gl
    _assert_all_tables_parity(db_bash, db_py)


def test_lr_fold_aged_penalty_decay_and_ordering_parity(tmp_path):
    """Subsumes .sh groups 7-8: an aged penalty (age=30, halflife=30 → decay 0.5)
    contributes ~half a fresh one, removing an extra ~0.25 of advantage with a
    smaller incremental drop than the fresh one (asserted on the LIVE bash side);
    bash==port on the fully-loaded state."""
    db = _init_db(tmp_path)
    _seed_region(db, "regionA")
    _seed_region(db, "regionB")
    fresh = [("codex_lens", "regionA", -0.5, 30.0, 0.0)]
    aged = fresh + [("codex_lens", "regionA", -0.5, 30.0, 30.0)]

    d_base = _derive(db, "base", [])
    d_fresh = _derive(db, "fresh", fresh)
    _recompute_bash(d_base)
    _recompute_bash(d_fresh)
    base = _radv(d_base, "codex_lens", "regionA")
    pre = _radv(d_fresh, "codex_lens", "regionA")

    d_aged = _derive(db, "aged", aged)
    db_bash, db_py = _recompute_pair(d_aged)
    after = _radv(db_bash, "codex_lens", "regionA")

    assert base is not None and pre is not None and after is not None
    # sh7: aged penalty halves the contribution → ~0.25 extra drop vs fresh-only
    assert abs((pre - after) - 0.25) < 5e-4, f"base={base} pre={pre} after={after}"
    # sh8: fresh drop (base→pre) larger than aged incremental drop (pre→after)
    assert abs(pre - after) < abs(base - pre), f"base={base} pre={pre} after={after}"
    _assert_all_tables_parity(db_bash, db_py)


def test_lr_fold_malformed_halflife_skipped_parity(tmp_path):
    """Subsumes .sh group 9: a malformed defect row (halflife=0.0) is skipped —
    not silently invented — so it neither crashes the recompute nor changes the
    lane's advantage (asserted on the LIVE bash side); bash==port parity."""
    db = _init_db(tmp_path)
    _seed_region(db, "regionA")
    _seed_region(db, "regionB")
    base_bash, _ = _recompute_pair(db)
    base_km_a = _radv(base_bash, "kimi_lens", "regionA")

    d = _derive(db, "malformed", [("kimi_lens", "regionA", -0.9, 0.0, 0.0)])
    db_bash, db_py = _recompute_pair(d)

    assert base_km_a is not None
    # sh9: malformed halflife=0.0 skipped — kimi_lens regionA unchanged
    assert _radv(db_bash, "kimi_lens", "regionA") == base_km_a
    _assert_all_tables_parity(db_bash, db_py)


# ── GRPO write-half refinement parity (subsumes retired
#    tests/unit/test_lane_router_refinements.sh) ──
#
# Provenance: that fixture drove lane_router_recompute_advantages over a canonical
# db/init.sh schema with 5 assertions, each toggling ONE knob against its legacy
# escape hatch to prove the knob moves the stored relative_advantage in the
# documented direction (so a vacuous port cannot pass):
#   (1) n-aware shrinkage — SHRINK_K=5 pulls a small-n (n=1) advantage below its
#       SHRINK_K=0 raw value;
#   (2) sigma-zero cost tie-break — on a flat-score group TIEBREAK=1 favours the
#       cheaper lane (adv>0) over the dearer (adv<0); TIEBREAK=0 leaves it at 0;
#   (3) EMA decay blend — a second recompute with DECAY_ALPHA=0.30 blends toward
#       the stored prior (0.30*batch2 + 0.70*batch1 = +0.20) instead of overwriting;
#   (4) recency weighting — HALFLIFE=14 down-weights an old opposing trace, lifting
#       the advantage above the HALFLIFE=0 (flat) value.
# Unlike the frc-a5 fold above, this fixture was LIVE at retirement (5 OK / 0 FAIL).
# Each case below re-seeds the scenario, runs the LIVE bash recompute and the port
# on identical DB copies with the SAME knobs, and asserts BOTH the .sh's directional
# behaviour (on the bash side — anti-vacuous) AND bash==port. That is strictly
# stronger than the .sh, which never cross-checked the port. The top parity gate
# pins HALFLIFE=0 and does a single recompute, so it never exercised the recency (4)
# or two-pass-EMA (3) paths these cases add.

_NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
_OLD = (datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
_TRACE_SEQ = itertools.count(1)  # monotonic trace_id counter, unique across the session


def _ins_trace(db, lane, rg, cost, created_at, region=""):
    """Insert one success trace (task_class='tc', objective_domain='code-delivery',
    node_type='implementer'), mirroring the retired refinements fixture's _ins."""
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


def _recompute_bash_knobs(db, knobs):
    subprocess.run(
        ["bash", "-c", f'. "{LR_SH}" && lane_router_recompute_advantages --since 0'],
        env={**os.environ, **knobs, "MINI_ORK_DB": db, "MO_STORE_DB": db},
        capture_output=True, text=True, check=True)


def _recompute_py_knobs(db, knobs):
    old = os.environ.copy()
    os.environ.update(knobs)
    try:
        lane_router.recompute_advantages(since=0, db=db)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_lr_ref_shrinkage_parity(tmp_path):
    """Subsumes refinements #1: SHRINK_K=5 pulls a small-n advantage below its
    SHRINK_K=0 raw value (LIVE bash side); bash==port."""
    db = _init_db(tmp_path, "shrink.db")
    _ins_trace(db, "laneA", 1.0, 1, _NOW)
    _ins_trace(db, "laneB", 0.0, 1, _NOW)
    common = {"MO_LEARNING_DECAY_ALPHA": "1.0", "MO_LEARNING_HALFLIFE_DAYS": "0",
              "MO_LEARNING_TIEBREAK": "0"}

    def pair(shrink_k):
        b, p = f"{db}.s{shrink_k}b", f"{db}.s{shrink_k}p"
        shutil.copy(db, b)
        shutil.copy(db, p)
        knobs = {**common, "MO_LEARNING_SHRINKAGE_K": str(shrink_k)}
        _recompute_bash_knobs(b, knobs)
        _recompute_py_knobs(p, knobs)
        return _gadv(b, "laneA", tc="tc"), _gadv(p, "laneA", tc="tc")

    shrunk_b, shrunk_p = pair(5)
    raw_b, raw_p = pair(0)
    assert shrunk_b is not None and raw_b is not None
    assert 0 < shrunk_b < raw_b and raw_b > 0.4, f"shrunk={shrunk_b} raw={raw_b}"
    assert shrunk_b == shrunk_p and raw_b == raw_p, \
        f"parity shrunk=({shrunk_b},{shrunk_p}) raw=({raw_b},{raw_p})"


def test_lr_ref_tiebreak_parity(tmp_path):
    """Subsumes refinements #2: on a flat-score group TIEBREAK=1 favours the
    cheaper lane over the dearer, TIEBREAK=0 leaves it at 0 (LIVE bash side);
    bash==port."""
    db = _init_db(tmp_path, "tie.db")
    _ins_trace(db, "laneC", 0.5, 1, _NOW)
    _ins_trace(db, "laneD", 0.5, 100, _NOW)
    common = {"MO_LEARNING_DECAY_ALPHA": "1.0", "MO_LEARNING_HALFLIFE_DAYS": "0",
              "MO_LEARNING_SHRINKAGE_K": "0"}

    def pair(tb):
        b, p = f"{db}.t{tb}b", f"{db}.t{tb}p"
        shutil.copy(db, b)
        shutil.copy(db, p)
        knobs = {**common, "MO_LEARNING_TIEBREAK": str(tb)}
        _recompute_bash_knobs(b, knobs)
        _recompute_py_knobs(p, knobs)
        return b, p

    b1, p1 = pair(1)
    c_b, d_b = _gadv(b1, "laneC", tc="tc"), _gadv(b1, "laneD", tc="tc")
    c_p, d_p = _gadv(p1, "laneC", tc="tc"), _gadv(p1, "laneD", tc="tc")
    b0, p0 = pair(0)
    c0_b, c0_p = _gadv(b0, "laneC", tc="tc"), _gadv(p0, "laneC", tc="tc")

    assert c_b is not None and d_b is not None and c0_b is not None
    assert c_b > d_b, f"TIEBREAK=1 cheaper C={c_b} !> dearer D={d_b}"
    assert -0.0001 <= c0_b <= 0.0001, f"TIEBREAK=0 flat group C={c0_b} != ~0"
    assert (c_b, d_b) == (c_p, d_p), f"TB1 parity bash=({c_b},{d_b}) py=({c_p},{d_p})"
    assert c0_b == c0_p, f"TB0 parity bash={c0_b} py={c0_p}"


def test_lr_ref_ema_blend_parity(tmp_path):
    """Subsumes refinements #3: a second recompute with DECAY_ALPHA=0.30 blends
    toward the stored prior (→ +0.20) rather than overwriting (LIVE bash side);
    bash==port. The top parity gate never exercises this two-pass EMA path."""
    db = _init_db(tmp_path, "ema.db")
    b, p = db + ".b", db + ".p"
    shutil.copy(db, b)
    shutil.copy(db, p)
    pass1 = {"MO_LEARNING_SHRINKAGE_K": "0", "MO_LEARNING_HALFLIFE_DAYS": "0",
             "MO_LEARNING_TIEBREAK": "0", "MO_LEARNING_DECAY_ALPHA": "1.0"}
    pass2 = {**pass1, "MO_LEARNING_DECAY_ALPHA": "0.30"}
    for dbx, recompute in ((b, _recompute_bash_knobs), (p, _recompute_py_knobs)):
        _ins_trace(dbx, "laneA", 1.0, 1, _NOW)
        _ins_trace(dbx, "laneB", 0.0, 1, _NOW)
        recompute(dbx, pass1)
        con = sqlite3.connect(dbx)   # drop only traces; keep the stored prior
        con.execute("DELETE FROM execution_traces")
        con.commit()
        con.close()
        _ins_trace(dbx, "laneA", 0.0, 1, _NOW)
        _ins_trace(dbx, "laneB", 1.0, 1, _NOW)
        recompute(dbx, pass2)

    blended_b = _gadv(b, "laneA", tc="tc")
    blended_p = _gadv(p, "laneA", tc="tc")
    assert blended_b is not None
    assert 0.1 < blended_b < 0.3, f"EMA blended bash={blended_b} (expect ~0.20)"
    assert blended_b == blended_p, f"EMA parity bash={blended_b} py={blended_p}"


def test_lr_ref_recency_parity(tmp_path):
    """Subsumes refinements #4: HALFLIFE=14 down-weights an old opposing trace,
    lifting laneA's advantage above the HALFLIFE=0 (flat) value (LIVE bash side);
    bash==port within 5e-4 (each side reads its own utcnow for the age). The top
    parity gate pins HALFLIFE=0, so this recency path was previously untested."""
    common = {"MO_LEARNING_SHRINKAGE_K": "0", "MO_LEARNING_DECAY_ALPHA": "1.0",
              "MO_LEARNING_TIEBREAK": "0"}

    def pair(hl):
        db = _init_db(tmp_path, f"rec{hl}.db")
        _ins_trace(db, "laneA", 1.0, 1, _NOW)
        _ins_trace(db, "laneA", 0.0, 1, _OLD)
        _ins_trace(db, "laneB", 0.5, 1, _NOW)
        b, p = db + ".b", db + ".p"
        shutil.copy(db, b)
        shutil.copy(db, p)
        knobs = {**common, "MO_LEARNING_HALFLIFE_DAYS": str(hl)}
        _recompute_bash_knobs(b, knobs)
        _recompute_py_knobs(p, knobs)
        return _gadv(b, "laneA", tc="tc"), _gadv(p, "laneA", tc="tc")

    rec_b, rec_p = pair(14)
    flat_b, flat_p = pair(0)
    assert rec_b is not None and flat_b is not None
    assert rec_b > flat_b, f"recency h14={rec_b} !> h0={flat_b}"
    assert abs(rec_b - rec_p) < 5e-4, f"recency parity bash={rec_b} py={rec_p}"
    assert flat_b == flat_p, f"flat parity bash={flat_b} py={flat_p}"
