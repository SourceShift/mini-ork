"""Artifact pass-rate reward for implementer traces (MO_ARTIFACT_REWARD).

Post-run, each successful implementer trace that still carries the exit-code
stamp is re-stamped with the pass rate of the run's ``verifier_results`` rows
recorded during its own attempt window — ``reward_source='artifact@v1'``.
Hermetic temp DB (``db/init.sh``), no network, no dispatch.
"""
from __future__ import annotations

import itertools
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.learning import writeback as wb  # noqa: E402

T0 = "2026-07-01T00:00:00Z"
E0 = 1782864000  # what strftime('%s', T0) returns
T1 = "2026-07-01T00:01:40Z"  # E0 + 100

_VR_SEQ = itertools.count()


def _seed_db(tmp, name):
    home = tmp / name / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        cwd=str(REPO),
        capture_output=True, text=True, check=True,
    )
    return db


def _sql(db, s):
    return subprocess.run(["sqlite3", db, s], capture_output=True, text=True)


def _insert_trace(db, tid, *, node_type="implementer", created_at=T0, run_id="r1",
                  status="success", validity=None, reward_source=None):
    cols = "trace_id,run_id,task_class,verifier_output,status,created_at"
    vals = (f"'{tid}','{run_id}','code_fix','{{\"node_type\":\"{node_type}\"}}',"
            f"'{status}','{created_at}'")
    if validity is not None:
        cols += ",validity"
        vals += f",'{validity}'"
    if reward_source is not None:
        cols += ",reward_source"
        vals += f",'{reward_source}'"
    r = _sql(db, f"INSERT INTO execution_traces ({cols}) VALUES ({vals});")
    assert r.returncode == 0, r.stderr


def _insert_vr(db, *, run_id="r1", verdict, created_at, fp=0, name="test"):
    rid = f"vr-{next(_VR_SEQ)}"
    r = _sql(
        db,
        "INSERT INTO verifier_results (result_id, run_id, verifier_name, verdict,"
        " is_false_positive, created_at) VALUES "
        f"('{rid}','{run_id}','{name}','{verdict}',{fp},{created_at});",
    )
    assert r.returncode == 0, r.stderr
    return rid


def _row(db, tid):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute(
            "SELECT * FROM execution_traces WHERE trace_id=?", (tid,)).fetchone()
    finally:
        con.close()
    return dict(r) if r is not None else None


@pytest.fixture(autouse=True)
def _reward_env_clean(monkeypatch):
    monkeypatch.delenv("MO_ARTIFACT_REWARD", raising=False)
    monkeypatch.delenv("MO_IMPL_REWARD", raising=False)


@pytest.fixture()
def _quiet_learning(monkeypatch):
    monkeypatch.setenv("MO_GRADE_RUN_REWARD", "0")
    monkeypatch.setenv("MO_LEARNING_WRITEBACK", "0")
    monkeypatch.setenv("MO_LANE_ROUTER", "0")


# -------------------------------------------------------------- helpers


def test_artifact_reward_helper():
    assert wb.artifact_reward(0, 0) is None
    assert wb.artifact_reward(3, 1) == 0.75


def test_module_constants():
    assert wb.ARTIFACT_REWARD_SOURCE == "artifact@v1"
    assert wb.ARTIFACT_REWARD_METRIC == "verifier_pass_rate"
    assert wb.ARTIFACT_REWARD_NODE_TYPES == ("implementer",)
    assert wb._EXIT_CODE_REWARD_SOURCES == ("", "verifier@v1", "artifact@v1")


# -------------------------------------------------------------- stamping


def test_stamp_pass_rate(tmp_path):
    db = _seed_db(tmp_path, "rate")
    _insert_trace(db, "a1")
    for v in ("pass", "pass", "fail", "pass"):
        _insert_vr(db, verdict=v, created_at=E0 + 10)
    assert wb.stamp_artifact_rewards(db, "r1") == 1
    row = _row(db, "a1")
    assert row["reward_value"] == 0.75
    assert row["reward_g"] == 0.5
    assert row["reward_anchor"] == 0.5
    assert row["reward_source"] == "artifact@v1"
    assert row["reward_primary_metric"] == "verifier_pass_rate"
    assert row["reward_direction"] == "higher_is_better"


def test_each_attempt_gets_its_own_window(tmp_path):
    db = _seed_db(tmp_path, "windows")
    _insert_trace(db, "a1", created_at=T0)
    _insert_trace(db, "a2", created_at=T1)
    for at in (E0 + 10, E0 + 20):
        _insert_vr(db, verdict="fail", created_at=at)
    for at in (E0 + 110, E0 + 120):
        _insert_vr(db, verdict="pass", created_at=at)
    assert wb.stamp_artifact_rewards(db, "r1") == 2
    assert _row(db, "a1")["reward_value"] == 0.0
    assert _row(db, "a1")["reward_g"] == -1.0
    assert _row(db, "a2")["reward_value"] == 1.0
    assert _row(db, "a2")["reward_g"] == 1.0


def test_earlier_rows_do_not_count(tmp_path):
    db = _seed_db(tmp_path, "earlier")
    _insert_trace(db, "a1")
    _insert_vr(db, verdict="fail", created_at=E0 - 5)
    assert wb.stamp_artifact_rewards(db, "r1") == 0
    row = _row(db, "a1")
    assert row["reward_source"] == "verifier@v1"
    assert row["reward_value"] is None


def test_indeterminate_and_vacuous_are_ignored(tmp_path):
    db = _seed_db(tmp_path, "noncommittal")
    _insert_trace(db, "a1")
    _insert_vr(db, verdict="indeterminate", created_at=E0 + 10)
    _insert_vr(db, verdict="vacuous", created_at=E0 + 20)
    before = _row(db, "a1")
    assert wb.stamp_artifact_rewards(db, "r1") == 0
    assert _row(db, "a1") == before


def test_false_positive_pass_is_a_fail(tmp_path):
    db = _seed_db(tmp_path, "fp")
    _insert_trace(db, "a1")
    _insert_vr(db, verdict="pass", fp=1, created_at=E0 + 10)
    _insert_vr(db, verdict="pass", fp=0, created_at=E0 + 20)
    assert wb.stamp_artifact_rewards(db, "r1") == 1
    assert _row(db, "a1")["reward_value"] == 0.5


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(status="failure"),
        dict(validity="infra_failed"),
        dict(reward_source="run_verified@v1"),
        dict(reward_source="eval@v1"),
        dict(node_type="reviewer"),
    ],
)
def test_ineligible_rows_are_untouched(tmp_path, kwargs):
    db = _seed_db(tmp_path, "ineligible")
    _insert_trace(db, "a1", **kwargs)
    _insert_vr(db, verdict="pass", created_at=E0 + 10)
    _insert_vr(db, verdict="fail", created_at=E0 + 20)
    before = _row(db, "a1")
    assert wb.stamp_artifact_rewards(db, "r1") == 0
    assert _row(db, "a1") == before


def test_other_runs_are_isolated(tmp_path):
    db = _seed_db(tmp_path, "isolation")
    _insert_trace(db, "a1")
    _insert_vr(db, run_id="r2", verdict="fail", created_at=E0 + 10)
    _insert_vr(db, run_id="r1", verdict="pass", created_at=E0 + 10)
    assert wb.stamp_artifact_rewards(db, "r1") == 1
    assert _row(db, "a1")["reward_value"] == 1.0


def test_idempotent(tmp_path):
    db = _seed_db(tmp_path, "idempotent")
    _insert_trace(db, "a1")
    _insert_vr(db, verdict="pass", created_at=E0 + 10)
    _insert_vr(db, verdict="fail", created_at=E0 + 20)
    assert wb.stamp_artifact_rewards(db, "r1") == 1
    first = _row(db, "a1")
    assert wb.stamp_artifact_rewards(db, "r1") == 1
    assert _row(db, "a1") == first


def test_fails_soft(tmp_path):
    db = _seed_db(tmp_path, "soft")
    _insert_trace(db, "a1")
    _insert_vr(db, verdict="pass", created_at=E0 + 10)
    before = _row(db, "a1")
    _sql(db, "DROP TABLE verifier_results;")
    assert wb.stamp_artifact_rewards(db, "r1") == 0
    assert _row(db, "a1") == before
    assert wb.stamp_artifact_rewards(str(tmp_path / "nope.db"), "r1") == 0
    assert wb.stamp_artifact_rewards(db, "") == 0


# ------------------------------------------------------- post-run wiring


def test_post_run_default_on(tmp_path, _quiet_learning):
    db = _seed_db(tmp_path, "post-on")
    _insert_trace(db, "a1")
    _insert_vr(db, verdict="fail", created_at=E0 + 1)
    ex._post_run_learning(db, str(tmp_path), "r1", "code_fix", fail_count=0)
    row = _row(db, "a1")
    assert row["reward_source"] == "artifact@v1"
    assert row["reward_value"] == 0.0


def test_post_run_opted_out(tmp_path, _quiet_learning, monkeypatch):
    monkeypatch.setenv("MO_ARTIFACT_REWARD", "0")
    db = _seed_db(tmp_path, "post-off")
    _insert_trace(db, "a1")
    _insert_vr(db, verdict="fail", created_at=E0 + 1)
    ex._post_run_learning(db, str(tmp_path), "r1", "code_fix", fail_count=0)
    row = _row(db, "a1")
    assert row["reward_source"] == "verifier@v1"
    assert row["reward_value"] is None


def test_post_run_run_verified_wins(tmp_path, _quiet_learning, monkeypatch):
    monkeypatch.setenv("MO_IMPL_REWARD", "run_verified")
    monkeypatch.setenv("MO_ROUTER_COST_LAMBDA", "0")
    db = _seed_db(tmp_path, "post-rv")
    _insert_trace(db, "a1")
    _insert_vr(db, verdict="fail", created_at=E0 + 1)
    ex._post_run_learning(db, str(tmp_path), "r1", "code_fix", fail_count=0)
    row = _row(db, "a1")
    assert row["reward_source"] == "run_verified@v1"
    assert row["reward_value"] == 1.0


def test_post_run_docstring_documents_step():
    doc = ex._post_run_learning.__doc__ or ""
    assert "0b" in doc
    assert "MO_ARTIFACT_REWARD" in doc
