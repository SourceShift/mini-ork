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


def _insert_trace(db, tid, av, status, cost, dur, validity=None, verdict="",
                  run_id="r1"):
    cols = ("trace_id,run_id,workflow_version_id,agent_version_id,task_class,"
            "prompt_version_hash,context_bundle_hash,tool_calls,files_read,"
            "files_written,verifier_output,reviewer_verdict,cost_usd,duration_ms,"
            "final_artifact_ref,status,created_at")
    vals = (f"'{tid}','{run_id}','wf1','{av}','code_fix','ph','ch','[]','[]','[]',"
            f"'{{\"node_type\":\"researcher\"}}','{verdict}',{cost},{dur},'','{status}',"
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
def _no_cn(monkeypatch):
    """write_grpo_advantages must never reach a live CN from a unit test — the
    graph projection is best-effort, so record the payloads here instead."""
    calls: list[tuple[list, list]] = []
    monkeypatch.setattr(
        "mini_ork.cn_client.graph_upsert_batched",
        lambda nodes, edges, source="mini-ork": calls.append((nodes, edges)),
    )
    return calls


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


def test_failure_status_scores_via_base_band_not_early_return(tmp_path):
    # F1 regression pin: the schema's status is 'failure' (singular — see the
    # execution_traces CHECK constraint, which _insert_trace enforces loudly).
    # reward() must score it through the designed base band (failure 0.15,
    # verdict can nudge ±0.10), never through the unknown-status early return
    # that flat-zeroed verdict-less failures and ranked approved failures
    # ABOVE successes at 1.0. Non-family lanes so the verdict band applies.
    db = _seed_db(tmp_path, "vocab")
    _insert_trace(db, "t1", "alpha_lane", "failure", 0.5, 800, verdict="approve")
    _insert_trace(db, "t2", "beta_lane", "failure", 0.5, 800)
    _insert_trace(db, "t3", "gamma_lane", "success", 1.0, 1000)
    write_grpo_advantages(db)
    # Designed rewards: approve-failure 0.25 < plain failure 0.15 < success 0.85
    # — a failure must never outrank a success, whatever the reviewer said.
    assert (float(_adv(db, "beta_lane"))
            < float(_adv(db, "alpha_lane"))
            < float(_adv(db, "gamma_lane")))


def test_projection_skips_empty_run_id(tmp_path, _no_cn):
    # Run / Trace / HAS_TRACE are projected from the rows already in hand. A row
    # with the empty run_id that 260 of 2650 live rows carry must project
    # nothing — an empty id would create a junk Run node and an edge to it.
    db = _seed_db(tmp_path, "proj")
    _insert_trace(db, "t1", "opus_lens", "success", 1.0, 1000, run_id="run-real-1")
    _insert_trace(db, "t2", "kimi_lens", "failure", 0.5, 800, run_id="")
    write_grpo_advantages(db)

    assert len(_no_cn) == 1, f"expected one batched emit, got {_no_cn}"
    nodes, edges = _no_cn[0]
    assert [n["id"] for n in nodes if n["label"] == "Run"] == ["run-real-1"]
    traces = {n["id"]: n for n in nodes if n["label"] == "Trace"}
    assert set(traces) == {"t1"}
    assert "t2" not in {n["id"] for n in nodes}
    # reward_g is reward(row): opus is a family lane (no verdict band), so a
    # success scores the 0.85 base.
    assert traces["t1"]["props"] == {"status": "success", "task_class": "code_fix",
                                     "reward_g": 0.85}
    assert edges == [{"from": "run-real-1", "from_label": "Run", "type": "HAS_TRACE",
                      "to": "t1", "to_label": "Trace", "props": {}}]
