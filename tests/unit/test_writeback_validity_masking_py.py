"""Reward hygiene: infra-exit validity masking in the GRPO advantage writeback.

A node that aborts for an infra reason the agent never controlled (watchdog
timeout, cost-circuit trip) must not pollute the group-relative advantage the
learning loop trains on. The producer stamps such rows validity='infra_failed'
(mini_ork.cli.execute), and write_grpo_advantages drops them from the group
aggregate. These tests pin both ends.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli.execute import is_non_learnable_exit
from mini_ork.learning.writeback import write_grpo_advantages


def _seed_db(tmp, name):
    home = tmp / name / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )
    return db


def _sql(db, s):
    return subprocess.run(["sqlite3", db, s], capture_output=True, text=True)


def _insert_trace(db, tid, av, status, cost, dur, validity=None):
    cols = ("trace_id,run_id,workflow_version_id,agent_version_id,task_class,"
            "prompt_version_hash,context_bundle_hash,tool_calls,files_read,"
            "files_written,verifier_output,reviewer_verdict,cost_usd,duration_ms,"
            "final_artifact_ref,status,created_at")
    vals = (f"'{tid}','r1','wf1','{av}','code_fix','ph','ch','[]','[]','[]',"
            f"'{{\"node_type\":\"researcher\"}}','',{cost},{dur},'','{status}',"
            f"'2026-07-01T00:00:00Z'")
    if validity is not None:
        cols += ",validity"
        vals += f",'{validity}'"
    r = _sql(db, f"INSERT INTO execution_traces ({cols}) VALUES ({vals});")
    # Fail loud on a rejected INSERT (e.g. a bad status enum) — a silently
    # dropped row would false-green every masking assertion below.
    assert r.returncode == 0, r.stderr


def _adv(db, av):
    return _sql(
        db,
        "SELECT printf('%.6f',relative_advantage) FROM agent_performance_memory "
        f"WHERE agent_version_id='{av}';",
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _det_env():
    # halflife=0 makes recency_weight deterministic (==1 for every row).
    old = dict(os.environ)
    os.environ["MO_LEARNING_HALFLIFE_DAYS"] = "0"
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_infra_row_is_invisible_to_advantage(tmp_path):
    # One (node_type, task_class) group. DB-A carries an extra infra_failed row
    # that must perturb NOTHING: the two valid lanes' advantages must match DB-B,
    # which has only the two valid rows, and the masked lane must write no row.
    a = _seed_db(tmp_path, "a")
    _insert_trace(a, "t1", "opus_lens", "success", 1.0, 1000)
    _insert_trace(a, "t2", "kimi_lens", "failure", 0.5, 800)
    _insert_trace(a, "t3", "minimax_lens", "failure", 0.9, 700, validity="infra_failed")
    write_grpo_advantages(a)

    b = _seed_db(tmp_path, "b")
    _insert_trace(b, "t1", "opus_lens", "success", 1.0, 1000)
    _insert_trace(b, "t2", "kimi_lens", "failure", 0.5, 800)
    write_grpo_advantages(b)

    assert _adv(a, "opus_lens") == _adv(b, "opus_lens") != ""
    assert _adv(a, "kimi_lens") == _adv(b, "kimi_lens") != ""
    # The infra-failed lane never entered a group → no advantage row.
    assert _adv(a, "minimax_lens") == ""


def test_legacy_empty_validity_counts(tmp_path):
    # Rows predating the stamp (empty-string validity) are treated as valid so
    # the historical corpus stays in the learning signal.
    db = _seed_db(tmp_path, "leg")
    _insert_trace(db, "t1", "opus_lens", "success", 1.0, 1000, validity="")
    _insert_trace(db, "t2", "kimi_lens", "failure", 0.5, 800, validity="")
    write_grpo_advantages(db)
    assert _adv(db, "opus_lens") != ""
    assert _adv(db, "kimi_lens") != ""


def test_all_infra_group_writes_nothing_and_does_not_raise(tmp_path):
    # If every row in a group is masked, the group produces no advantage row and
    # the div-by-zero guard is never reached (no exception).
    db = _seed_db(tmp_path, "allinfra")
    _insert_trace(db, "t1", "opus_lens", "failure", 1.0, 1000, validity="infra_failed")
    _insert_trace(db, "t2", "kimi_lens", "failure", 0.5, 800, validity="infra_failed")
    n = write_grpo_advantages(db)
    assert n == 0
    assert _adv(db, "opus_lens") == ""
    assert _adv(db, "kimi_lens") == ""


def test_is_non_learnable_exit_predicate():
    # Producer side: only the two unambiguous infra finish_reasons mask.
    assert is_non_learnable_exit("timeout")
    assert is_non_learnable_exit("cost_limit")
    assert is_non_learnable_exit("COST_LIMIT")
    # Capability failures and normal outcomes stay learnable.
    assert not is_non_learnable_exit("error")
    assert not is_non_learnable_exit("done")
    assert not is_non_learnable_exit("")
    assert not is_non_learnable_exit("success")
