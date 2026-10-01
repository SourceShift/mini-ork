"""Implementer run-verified stamp (MO_IMPL_REWARD=run_verified).

Stamps the per-row reward on a run's implementer rows with ``run_verified@v1``
= (verified?1.0:0.0) - lam*cost/ref, clamped to [-1.0, 1.0]; reward_anchor=0.5;
reward_g = (v - 0.5) / 0.5. Opt-in: only fires when ``MO_IMPL_REWARD=run_verified``
AND ``fail_count`` is supplied to ``_post_run_learning``.

Branches pinned:
  * flag unset → rows untouched.
  * verified=True / False, λ=0 → reward_value=1.0/0.0, reward_g=1.0/-1.0.
  * λ=0.1, ref=0.20, costs 0.05 vs 0.15 → Δreward_g == 0.1.
  * non-'valid' validity on ANY row of the run → implementer rows masked
    ``validity='infra_failed'``, reward untouched, return 0.
  * no implementer rows → return 0, no writes.
  * non-implementer rows of the run are never modified.
  * integration: ``_post_run_learning(fail_count=0)`` with the master flag
    ON + sibling steps disabled stamps the row; ``fail_count=None`` does not.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.learning.writeback import stamp_impl_run_verified  # noqa: E402


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


def _col(db, tid, col):
    r = _sql(db, f"SELECT printf('%.6f',{col}) FROM execution_traces "
                 f"WHERE trace_id='{tid}';")
    out = r.stdout.strip()
    return float(out) if out else None


def _source(db, tid):
    r = _sql(db, f"SELECT reward_source FROM execution_traces WHERE trace_id='{tid}';")
    return r.stdout.strip()


def _validity(db, tid):
    r = _sql(db, f"SELECT validity FROM execution_traces WHERE trace_id='{tid}';")
    return r.stdout.strip()


def _insert_trace(db, tid, *, node_type, cost_usd, run_id="r1", status="success",
                  validity=None):
    cols = ("trace_id,run_id,workflow_version_id,agent_version_id,task_class,"
            "prompt_version_hash,context_bundle_hash,tool_calls,files_read,"
            "files_written,verifier_output,reviewer_verdict,cost_usd,duration_ms,"
            "final_artifact_ref,status,created_at")
    vals = (f"'{tid}','{run_id}','wf1','codex_lens','code_fix','ph','ch','[]','[]','[]',"
            f"'{{\"node_type\":\"{node_type}\"}}','',{cost_usd},1000,'','{status}',"
            f"'2026-07-01T00:00:00Z'")
    if validity is not None:
        cols += ",validity"
        vals += f",'{validity}'"
    r = _sql(db, f"INSERT INTO execution_traces ({cols}) VALUES ({vals});")
    assert r.returncode == 0, r.stderr


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with the impl-reward env knobs unset."""
    for key in ("MO_IMPL_REWARD", "MO_ROUTER_COST_LAMBDA", "MO_ROUTER_COST_REF"):
        monkeypatch.delenv(key, raising=False)
    yield


def test_flag_unset_leaves_rows_untouched(tmp_path, monkeypatch):
    """``MO_IMPL_REWARD`` unset → ``_post_run_learning`` never calls the stamp."""
    monkeypatch.setenv("MO_GRADE_RUN_REWARD", "0")
    monkeypatch.setenv("MO_LEARNING_WRITEBACK", "0")
    monkeypatch.setenv("MO_LANE_ROUTER", "0")
    db = _seed_db(tmp_path, "flag-off")
    _insert_trace(db, "impl1", node_type="implementer", cost_usd=0.10)
    ex._post_run_learning(db, str(tmp_path), "r1", "code_fix", fail_count=0)
    # The stamp distinguisher is reward_source: the migration default is
    # 'verifier@v1'; only our stamp writes 'run_verified@v1'.
    assert _source(db, "impl1") != "run_verified@v1"


def test_verified_true_lambda_zero_stamps_one(tmp_path):
    os.environ["MO_ROUTER_COST_LAMBDA"] = "0"
    db = _seed_db(tmp_path, "vtrue")
    _insert_trace(db, "impl1", node_type="implementer", cost_usd=0.10)
    n = stamp_impl_run_verified(db, "r1", verified=True)
    assert n == 1
    assert _source(db, "impl1") == "run_verified@v1"
    assert _col(db, "impl1", "reward_value") == pytest.approx(1.0)
    assert _col(db, "impl1", "reward_anchor") == pytest.approx(0.5)
    assert _col(db, "impl1", "reward_g") == pytest.approx(1.0)


def test_verified_false_lambda_zero_stamps_zero(tmp_path):
    os.environ["MO_ROUTER_COST_LAMBDA"] = "0"
    db = _seed_db(tmp_path, "vfalse")
    _insert_trace(db, "impl1", node_type="implementer", cost_usd=0.10)
    n = stamp_impl_run_verified(db, "r1", verified=False)
    assert n == 1
    assert _source(db, "impl1") == "run_verified@v1"
    assert _col(db, "impl1", "reward_value") == pytest.approx(0.0)
    assert _col(db, "impl1", "reward_anchor") == pytest.approx(0.5)
    assert _col(db, "impl1", "reward_g") == pytest.approx(-1.0)


def test_cost_penalty_arithmetic(tmp_path):
    """λ=0.1, ref=0.20, cost 0.05 vs 0.15 → Δreward_g == 0.1 exactly."""
    os.environ["MO_ROUTER_COST_LAMBDA"] = "0.1"
    db = _seed_db(tmp_path, "cost")
    _insert_trace(db, "cheap", node_type="implementer", cost_usd=0.05, run_id="rc")
    _insert_trace(db, "rich", node_type="implementer", cost_usd=0.15, run_id="rc")
    n = stamp_impl_run_verified(db, "rc", verified=True)
    assert n == 2
    rg_cheap = _col(db, "cheap", "reward_g")
    rg_rich = _col(db, "rich", "reward_g")
    assert rg_cheap is not None and rg_rich is not None
    delta = rg_rich - rg_cheap
    assert delta == pytest.approx(-0.1, abs=1e-6)
    assert rg_cheap > rg_rich  # cheaper lane gets higher reward_g


def test_infra_mask_when_other_row_has_timeout_validity(tmp_path):
    db = _seed_db(tmp_path, "infra")
    _insert_trace(db, "impl1", node_type="implementer", cost_usd=0.10)
    _insert_trace(db, "test1", node_type="verifier", cost_usd=0.05,
                  validity="timeout")
    n = stamp_impl_run_verified(db, "r1", verified=True)
    assert n == 0
    assert _validity(db, "impl1") == "infra_failed"
    assert _source(db, "impl1") != "run_verified@v1"
    # Non-implementer row was NEVER touched.
    assert _validity(db, "test1") == "timeout"


def test_no_implementer_rows_returns_zero(tmp_path):
    db = _seed_db(tmp_path, "no-impl")
    _insert_trace(db, "test1", node_type="verifier", cost_usd=0.05)
    _insert_trace(db, "rev1", node_type="reviewer", cost_usd=0.05)
    n = stamp_impl_run_verified(db, "r1", verified=True)
    assert n == 0
    assert _source(db, "test1") != "run_verified@v1"
    assert _source(db, "rev1") != "run_verified@v1"


def test_non_implementer_rows_never_modified(tmp_path):
    db = _seed_db(tmp_path, "mixed")
    _insert_trace(db, "impl1", node_type="implementer", cost_usd=0.10)
    _insert_trace(db, "impl2", node_type="implementer", cost_usd=0.20)
    _insert_trace(db, "test1", node_type="verifier", cost_usd=0.05)
    _insert_trace(db, "rev1", node_type="reviewer", cost_usd=0.05)
    n = stamp_impl_run_verified(db, "r1", verified=True)
    assert n == 2
    # Non-implementer rows: stamp distinguisher (source) is the migration default,
    # never our 'run_verified@v1'. reward_value is 0.0 by migration default so
    # checking it as 'None' would false-fail.
    for tid in ("test1", "rev1"):
        assert _source(db, tid) != "run_verified@v1"


def test_post_run_learning_stamps_with_fail_count_zero(tmp_path, monkeypatch):
    os.environ["MO_IMPL_REWARD"] = "run_verified"
    # Sibling steps off so the assertion is not poisoned.
    monkeypatch.setenv("MO_GRADE_RUN_REWARD", "0")
    monkeypatch.setenv("MO_LEARNING_WRITEBACK", "0")
    monkeypatch.setenv("MO_LANE_ROUTER", "0")
    monkeypatch.setenv("MO_AUTO_APPLY", "0")
    monkeypatch.setenv("MO_APPLY_ENABLED", "0")

    db = _seed_db(tmp_path, "live")
    _insert_trace(db, "impl1", node_type="implementer", cost_usd=0.10)
    ex._post_run_learning(db, str(tmp_path), "r1", "code_fix", fail_count=0)
    assert _source(db, "impl1") == "run_verified@v1"
    assert _col(db, "impl1", "reward_value") == pytest.approx(1.0)


def test_post_run_learning_skips_when_fail_count_none(tmp_path, monkeypatch):
    os.environ["MO_IMPL_REWARD"] = "run_verified"
    monkeypatch.setenv("MO_GRADE_RUN_REWARD", "0")
    monkeypatch.setenv("MO_LEARNING_WRITEBACK", "0")
    monkeypatch.setenv("MO_LANE_ROUTER", "0")
    monkeypatch.setenv("MO_AUTO_APPLY", "0")
    monkeypatch.setenv("MO_APPLY_ENABLED", "0")

    db = _seed_db(tmp_path, "no-fc")
    _insert_trace(db, "impl1", node_type="implementer", cost_usd=0.10)
    ex._post_run_learning(db, str(tmp_path), "r1", "code_fix", fail_count=None)
    assert _source(db, "impl1") != "run_verified@v1"
