"""Unit tests for ``mini_ork.learning.contrastive_lanes`` (MGM lane mining).

Hermetic: fresh migrated temp sqlite, no network, no model. Covers the
contrastive-pair rules, summarisation, mining/upsert semantics, the existing
verify path the mined rows feed, and the reflect side-channel wiring.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mini_ork.learning import contrastive_lanes
from mini_ork.stores import migrate

REPO = Path(__file__).resolve().parents[2]
REPO_ROOT = str(REPO)


@pytest.fixture()
def db(tmp_path):
    """Copy of the ``test_pattern_store_py.py`` db fixture (full migrated schema)."""
    dbp = str(tmp_path / "state.db")
    rc, out, err = migrate.init_db(db=dbp, root=REPO_ROOT)
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    return dbp


def _created(days_old: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _trace(con, trace_id, run_id, lane, status, *, node_type="reviewer",
           node_id="reviewer", finish_reason="done",
           task_class="framework_edit", days_old=1, verifier_output=None):
    if verifier_output is None:
        verifier_output = json.dumps(
            {"node_type": node_type, "finish_reason": finish_reason})
    con.execute(
        "INSERT INTO execution_traces (trace_id, run_id, task_class,"
        " agent_version_id, status, verifier_output, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (trace_id, run_id, task_class, lane, status, verifier_output,
         _created(days_old)))
    con.commit()


def _pair(con, run, *, winner="glm", loser="opus_lens", reason="no_op",
          suffix="01", days_old=1):
    w = f"tr-reviewer-reviewer-00000a{suffix}"
    l = f"tr-reviewer-reviewer-00000b{suffix}"
    _trace(con, w, run, winner, "success", days_old=days_old)
    _trace(con, l, run, loser, "failure", finish_reason=reason,
           days_old=days_old)
    return w, l


def _pid():
    return contrastive_lanes.pattern_id_for(
        "framework_edit", "reviewer", "reviewer", "glm", "opus_lens")


def _row(db_path, pid):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT pattern_id, description, evidence_trace_ids, frequency,"
            " output_type, status, lesson_text, promoted_to FROM pattern_records"
            " WHERE pattern_id=?", (pid,)).fetchone()
    finally:
        con.close()


# ── constants and small helpers ─────────────────────────────────────────────

def test_constants():
    assert contrastive_lanes.EXCLUDED_REASONS == frozenset(
        {"verdict_revise", "verdict_fail", "impl_moved_base"})
    # The engine's non-learnable exits (execute._NON_LEARNABLE_FINISH_REASONS).
    assert contrastive_lanes.INFRA_REASONS == frozenset({"timeout", "cost_limit"})
    assert contrastive_lanes.MIN_RUNS_FLOOR == 3


def test_enabled(monkeypatch):
    monkeypatch.delenv("MO_MGM_MINER", raising=False)
    assert contrastive_lanes.enabled() is True
    monkeypatch.setenv("MO_MGM_MINER", "0")
    assert contrastive_lanes.enabled() is False


def test_pattern_id_for_formula():
    import hashlib
    expected = "pat-mgm-" + hashlib.sha256(
        "framework_edit|reviewer|reviewer|glm|opus_lens".encode()
    ).hexdigest()[:12]
    assert _pid() == expected


def test_describe_template_and_contracts():
    from mini_ork.context_assembler import pattern_task_class
    key = {"task_class": "framework_edit", "node_type": "reviewer",
           "node_id": "reviewer", "winner": "glm", "loser": "opus_lens",
           "n_runs": 3, "reasons": ["no_op"]}
    desc = contrastive_lanes.describe(key)
    assert desc == (
        "cluster: task_class=framework_edit contrastive node=reviewer/reviewer "
        "winner=glm loser=opus_lens (runs=3): in each run the glm attempt "
        "succeeded where the opus_lens attempt at the same node failed (no_op); "
        "the lesson is what the succeeding attempt did that the failing one did not")
    assert desc.startswith("cluster: task_class=framework_edit ")
    assert "status=failure" not in desc
    assert "status=vacuous" not in desc
    assert pattern_task_class(desc) == "framework_edit"
    # "" renders as unknown; long labels truncate at 480.
    assert "(unknown)" in contrastive_lanes.describe(dict(key, reasons=[""]))
    long = contrastive_lanes.describe(dict(
        key, n_runs=99,
        reasons=[f"r{i}" + "x" * 90 for i in range(8)]))
    assert len(long) == 480


# ── find_pairs ──────────────────────────────────────────────────────────────

def test_find_pairs_one_quality_pair(db):
    con = sqlite3.connect(db)
    w, l = _pair(con, "r1")
    pairs = contrastive_lanes.find_pairs(con)
    con.close()
    assert len(pairs) == 1
    p = pairs[0]
    assert p["winner"] == "glm" and p["loser"] == "opus_lens"
    assert p["kind"] == "quality"
    assert p["loser_reasons"] == ["no_op"]
    assert p["winner_trace_ids"] == [w]
    assert p["loser_trace_ids"] == [l]


def test_find_pairs_no_self_pair(db):
    con = sqlite3.connect(db)
    _trace(con, "tr-reviewer-reviewer-00000001", "r1", "glm", "success")
    _trace(con, "tr-reviewer-reviewer-00000002", "r1", "glm", "failure",
           finish_reason="no_op")
    assert contrastive_lanes.find_pairs(con) == []
    con.close()


@pytest.mark.parametrize("reason", ["verdict_revise", "verdict_fail",
                                    "impl_moved_base"])
def test_find_pairs_excluded_reasons(db, reason):
    con = sqlite3.connect(db)
    _trace(con, "tr-reviewer-reviewer-00000001", "r1", "glm", "success")
    _trace(con, "tr-reviewer-reviewer-00000002", "r1", "opus_lens", "failure",
           finish_reason=reason)
    assert contrastive_lanes.find_pairs(con) == []
    con.close()


def test_find_pairs_infra_kind(db):
    con = sqlite3.connect(db)
    _pair(con, "r1", reason="timeout")
    pairs = contrastive_lanes.find_pairs(con)
    con.close()
    assert pairs[0]["kind"] == "infra"
    assert pairs[0]["loser_reasons"] == ["timeout"]


@pytest.mark.parametrize("reason", ["error", ""])
def test_find_pairs_error_and_empty_reason_are_quality(db, reason):
    # Review fix: `error` and an empty finish_reason are valid, learnable
    # failures (1,236 of 1,732 live failures), not plumbing.
    con = sqlite3.connect(db)
    _pair(con, "r1", reason=reason)
    pairs = contrastive_lanes.find_pairs(con)
    con.close()
    assert pairs[0]["kind"] == "quality"


def test_find_pairs_infra_failed_validity_is_infra(db):
    # A trace the engine labelled validity='infra_failed' is infra even when its
    # finish_reason is empty.
    con = sqlite3.connect(db)
    _w, loser = _pair(con, "r1", reason="")
    con.execute("UPDATE execution_traces SET validity='infra_failed' WHERE trace_id=?",
                (loser,))
    con.commit()
    pairs = contrastive_lanes.find_pairs(con)
    con.close()
    assert pairs[0]["kind"] == "infra"


@pytest.mark.parametrize("bad", [
    ("tr-plan-1793-42", "r1", "opus_lens", "failure", None, "planner"),
    ("tr-planner-reviewer-00000001", "r1", "opus_lens", "failure", None,
     "planner"),
    ("tr-reviewer-reviewer-00000002", "r1", "  ", "failure", None, "reviewer"),
    ("tr-reviewer-reviewer-00000003", "", "opus_lens", "failure", None,
     "reviewer"),
    ("tr-reviewer-reviewer-00000004", "r1", "opus_lens", "failure", '"x"',
     "reviewer"),
])
def test_find_pairs_ignores_malformed_rows(db, bad):
    tid, run, lane, status, vo_raw, nt = bad
    con = sqlite3.connect(db)
    _trace(con, "tr-reviewer-reviewer-00000001", "r1", "glm", "success")
    kwargs = {"node_type": nt}
    if vo_raw is not None:
        kwargs["verifier_output"] = vo_raw
    _trace(con, tid, run, lane, status, **kwargs)
    assert contrastive_lanes.find_pairs(con) == []
    con.close()


def test_find_pairs_same_run_only(db):
    con = sqlite3.connect(db)
    _trace(con, "tr-reviewer-reviewer-00000001", "r1", "glm", "success")
    _trace(con, "tr-reviewer-reviewer-00000002", "r2", "opus_lens", "failure",
           finish_reason="no_op")
    assert contrastive_lanes.find_pairs(con) == []
    con.close()


def test_find_pairs_since_iso_filter(db):
    con = sqlite3.connect(db)
    _pair(con, "r1", days_old=40)
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    assert contrastive_lanes.find_pairs(con, since_iso=since) == []
    assert len(contrastive_lanes.find_pairs(con, since_iso=None)) == 1
    con.close()


def test_find_pairs_missing_table_returns_empty(tmp_path):
    con = sqlite3.connect(str(tmp_path / "bare.db"))
    con.execute("CREATE TABLE unrelated (x TEXT)")
    assert contrastive_lanes.find_pairs(con) == []
    con.close()


# ── summarize ───────────────────────────────────────────────────────────────

def test_summarize_three_runs(db):
    con = sqlite3.connect(db)
    expected = []
    for i, run in enumerate(("r1", "r2", "r3"), 1):
        w2 = f"tr-reviewer-reviewer-0000a{i}02"
        w = f"tr-reviewer-reviewer-0000a{i}01"
        l = f"tr-reviewer-reviewer-0000b{i}01"
        _trace(con, w2, run, "glm", "success")
        _trace(con, w, run, "glm", "success")
        _trace(con, l, run, "opus_lens", "failure", finish_reason="no_op")
        expected += sorted([w, w2]) + [l]
    keys = contrastive_lanes.summarize(contrastive_lanes.find_pairs(con))
    con.close()
    assert len(keys) == 1
    k = keys[0]
    assert k["n_runs"] == 3
    assert k["run_ids"] == ["r1", "r2", "r3"]
    assert k["kind"] == "quality"
    assert k["trace_ids"] == expected


def test_summarize_mixed_kind_is_quality(db):
    con = sqlite3.connect(db)
    _pair(con, "r1", reason="no_op", suffix="01")
    _pair(con, "r2", reason="", suffix="02")
    keys = contrastive_lanes.summarize(contrastive_lanes.find_pairs(con))
    con.close()
    assert keys[0]["kind"] == "quality"
    assert keys[0]["reasons"] == ["", "no_op"]


# ── mine_into_patterns ──────────────────────────────────────────────────────

@pytest.fixture()
def miner_on(monkeypatch):
    for var in ("MO_MGM_MINER", "MO_MGM_MINER_WINDOW", "MO_MGM_MINER_MIN_RUNS"):
        monkeypatch.delenv(var, raising=False)


def _seed_runs(con, runs=("r1", "r2", "r3"), reason="no_op"):
    ids = []
    for i, run in enumerate(runs, 1):
        w = f"tr-reviewer-reviewer-0000a{i}01"
        l = f"tr-reviewer-reviewer-0000b{i}01"
        _trace(con, w, run, "glm", "success")
        _trace(con, l, run, "opus_lens", "failure", finish_reason=reason)
        ids += [w, l]
    return ids


def test_missing_db_zero_report_no_file(tmp_path, miner_on):
    missing = str(tmp_path / "nope" / "state.db")
    assert contrastive_lanes.mine_into_patterns(db_path=missing) == {
        "enabled": True, "pairs": 0, "keys": 0, "quality_keys": 0,
        "written": 0, "held": 0, "routing": []}
    assert not os.path.exists(missing)


def test_disabled_returns_zero_report(db, monkeypatch):
    con = sqlite3.connect(db)
    _seed_runs(con)
    con.close()
    monkeypatch.setenv("MO_MGM_MINER", "0")
    report = contrastive_lanes.mine_into_patterns(db_path=db)
    assert report["enabled"] is False and report["written"] == 0
    assert _row(db, _pid()) is None


def test_row_written_at_floor(db, miner_on):
    con = sqlite3.connect(db)
    ids = _seed_runs(con)
    con.close()
    report = contrastive_lanes.mine_into_patterns(db_path=db)
    assert report["written"] == 1 and report["held"] == 0
    assert report["pairs"] == 3 and report["keys"] == 1
    assert report["quality_keys"] == 1 and report["routing"] == []
    row = _row(db, _pid())
    assert row[3] == 3
    assert row[4] == "best_practice_rule"
    assert row[5] == "observed"
    assert row[6] is None
    assert row[1] == contrastive_lanes.describe({
        "task_class": "framework_edit", "node_type": "reviewer",
        "node_id": "reviewer", "winner": "glm", "loser": "opus_lens",
        "n_runs": 3, "reasons": ["no_op"]})
    assert json.loads(row[2]) == ids


def test_below_floor_and_clamp(db, miner_on):
    con = sqlite3.connect(db)
    _seed_runs(con, runs=("r1", "r2"))
    con.close()
    for min_runs in (3, 1, "1"):
        report = contrastive_lanes.mine_into_patterns(
            db_path=db, min_runs=min_runs)
        assert report["held"] == 1 and report["written"] == 0
    assert _row(db, _pid()) is None


def test_floor_can_be_raised_and_bad_value_defaults(db, miner_on):
    con = sqlite3.connect(db)
    _seed_runs(con)
    con.close()
    assert contrastive_lanes.mine_into_patterns(
        db_path=db, min_runs=5)["held"] == 1
    assert contrastive_lanes.mine_into_patterns(
        db_path=db, min_runs="not-a-number")["written"] == 1


def test_infra_never_written(db, miner_on):
    con = sqlite3.connect(db)
    _seed_runs(con, reason="timeout")
    con.close()
    report = contrastive_lanes.mine_into_patterns(db_path=db)
    assert report["written"] == 0 and report["held"] == 0
    assert report["quality_keys"] == 0
    assert _row(db, _pid()) is None
    assert len(report["routing"]) == 1
    assert report["routing"][0]["n_runs"] == 3
    assert report["routing"][0]["reasons"] == ["timeout"]


def test_remining_merges_and_preserves_lesson(db, miner_on):
    con = sqlite3.connect(db)
    ids3 = _seed_runs(con)
    con.close()
    contrastive_lanes.mine_into_patterns(db_path=db)
    assert contrastive_lanes.mine_into_patterns(db_path=db)["written"] == 1
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT COUNT(*) FROM pattern_records").fetchone()[0] == 1
    con.execute("UPDATE pattern_records SET lesson_text='When x: y',"
                " status='dismissed', promoted_to='wc-1' WHERE pattern_id=?",
                (_pid(),))
    con.commit()
    _trace(con, "tr-reviewer-reviewer-00000a04", "r4", "glm", "success")
    _trace(con, "tr-reviewer-reviewer-00000b04", "r4", "opus_lens", "failure",
           finish_reason="no_op")
    con.close()
    assert contrastive_lanes.mine_into_patterns(db_path=db)["written"] == 1
    row = _row(db, _pid())
    assert row[3] == 4
    assert json.loads(row[2]) == ids3 + [
        "tr-reviewer-reviewer-00000a04", "tr-reviewer-reviewer-00000b04"]
    assert row[6] == "When x: y"


def test_update_merges_bad_json_evidence(db, miner_on):
    con = sqlite3.connect(db)
    ids = _seed_runs(con)
    con.close()
    contrastive_lanes.mine_into_patterns(db_path=db)
    con = sqlite3.connect(db)
    con.execute("UPDATE pattern_records SET evidence_trace_ids='not-json'"
                " WHERE pattern_id=?", (_pid(),))
    con.commit()
    con.close()
    contrastive_lanes.mine_into_patterns(db_path=db)
    assert json.loads(_row(db, _pid())[2]) == ids


def test_window(db, miner_on):
    con = sqlite3.connect(db)
    _pair(con, "r1", days_old=40)
    con.close()
    excluded = contrastive_lanes.mine_into_patterns(db_path=db, window="30d")
    assert excluded["pairs"] == 0 and excluded["written"] == 0
    for window in (None, "", "bogus", "7w"):
        assert contrastive_lanes.mine_into_patterns(
            db_path=db, window=window)["pairs"] == 1


# ── end-to-end through the existing verify path ─────────────────────────────

def _run_verify_path(db_path):
    from mini_ork.learning import reflection_pipeline as rp
    suggestions = rp.reflection_suggest_promotions(db_path=db_path)
    rp.reflection_persist_suggestions(json.dumps(suggestions), db_path=db_path)
    rp.reflection_verify_patterns(db_path=db_path)


@pytest.fixture()
def verify_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_PROMOTION_MIN_FREQ", "3")
    for var in ("MO_EMERGENT_VERIFY", "MO_EMERGENT_VERIFY_MIN_STRENGTH",
                "MO_EMERGENT_VERIFY_MIN_EVIDENCE",
                "MO_EMERGENT_VERIFY_REQUIRE_LESSON"):
        monkeypatch.delenv(var, raising=False)


def _emergent(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT status, task_class FROM emergent_patterns"
            " WHERE pattern_id=?", (_pid(),)).fetchone()
    finally:
        con.close()


def test_verify_path_no_lesson_holds(db, miner_on, verify_env):
    con = sqlite3.connect(db)
    _seed_runs(con)
    con.close()
    assert contrastive_lanes.mine_into_patterns(db_path=db)["written"] == 1
    _run_verify_path(db)
    row = _emergent(db)
    assert row is not None
    assert row[0] == "proposed"
    assert row[1] == "framework_edit"


def test_verify_path_approves_with_lesson(db, miner_on, verify_env):
    con = sqlite3.connect(db)
    _seed_runs(con)
    con.close()
    contrastive_lanes.mine_into_patterns(db_path=db)
    _run_verify_path(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE pattern_records SET"
                " lesson_text='When reviewing: emit a verdict file'"
                " WHERE pattern_id=?", (_pid(),))
    con.commit()
    con.close()
    _run_verify_path(db)
    row = _emergent(db)
    assert row is not None and row[0] == "approved"
    assert row[1] == "framework_edit"


# ── reflect wiring ──────────────────────────────────────────────────────────

FIXED_SINCE = str(int(time.time()))


@pytest.fixture()
def temp_db(tmp_path):
    dbp = str(tmp_path / "reflect.db")
    rc, _out, err = migrate.init_db(db=dbp, root=REPO_ROOT)
    assert rc == 0, f"init_db failed: {err}"
    con = sqlite3.connect(dbp)
    for i, run in enumerate(("r1", "r2", "r3"), 1):
        created = _created(1)
        for lane, status, reason, suffix in (
                ("glm", "success", "done", "a"),
                ("opus_lens", "failure", "no_op", "b")):
            con.execute(
                "INSERT INTO execution_traces (trace_id, run_id, task_class,"
                " agent_version_id, status, verifier_output, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"tr-reviewer-reviewer-00000{suffix}0{i}", run,
                 "framework_edit", lane, status,
                 json.dumps({"node_type": "reviewer",
                             "finish_reason": reason}), created))
    con.commit()
    con.close()
    return dbp


def _run_py(db_path, tmp_path, env_extra=None):
    env = dict(os.environ)
    env.update({
        "MINI_ORK_DB": db_path,
        "MINI_ORK_HOME": str(tmp_path),
        "MINI_ORK_ROOT": REPO_ROOT,
        "PYTHONPATH": REPO_ROOT,
        "MO_CODE_FINDINGS": "0",
        "MO_CROSS_EPIC_GRADIENTS": "0",
        "MO_BUG_REPORT_SWEEP": "0",
        "MO_RHO_AGGREGATE": "0",
        "MO_LANE_ROUTER": "0",
        "MO_PATTERN_INDUCE": "0",
        "MO_PATTERN_MINER": "1",
        "MINI_ORK_GRADIENT_EXTRACTOR_FN": "_rfl_stub",
    })
    for var in ("MO_MGM_MINER", "MO_MGM_MINER_WINDOW", "MO_MGM_MINER_MIN_RUNS",
                "MINI_ORK_RUN_DIR"):
        env.pop(var, None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.reflect", "--since", FIXED_SINCE],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=180)


def test_reflect_prints_and_mines(temp_db, tmp_path):
    proc = _run_py(temp_db, tmp_path)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "[mgm_miner]" in proc.stdout
    assert _row(temp_db, _pid())[3] == 3


def test_reflect_opt_out(temp_db, tmp_path):
    proc = _run_py(temp_db, tmp_path, env_extra={"MO_MGM_MINER": "0"})
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "[mgm_miner]" not in proc.stdout
    assert "[pattern_miner]" in proc.stdout
