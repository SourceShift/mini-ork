"""Parity gate: mini_ork.trace_store vs lib/trace_store.sh, on the real schema.

For each payload we write via the LIVE bash `trace_write` (trace_id bash-N) and
via the Python `trace_write` (trace_id py-N) into the same DB, then read back and
assert reward_g (+ core columns) match. No mocking, no hardcoded reward_g.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402

TS_SH = REPO / "lib" / "trace_store.sh"

PAYLOADS = [
    {"task_class": "code-fix", "status": "success", "reward_value": 1.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
    {"task_class": "code-fix", "status": "failure", "reward_value": 0.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
    {"task_class": "code-fix", "status": "success", "reward_value": 0.5, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
    {"task_class": "book-gen", "status": "success", "reward_value": 2.0, "reward_anchor": 1.0, "reward_direction": "higher_is_better"},
    {"task_class": "book-gen", "status": "success", "reward_value": 1.0, "reward_anchor": 2.0, "reward_direction": "lower_is_better"},
    {"task_class": "code-fix", "status": "success", "reward_value": 1.0, "reward_anchor": 0.0, "reward_direction": "higher_is_better"},
    {"task_class": "code-fix", "status": "success"},
    {"task_class": "eval", "status": "success", "reward_value": 3.0, "reward_anchor": 1.5, "reward_direction": "higher_is_better"},
    {"task_class": "eval", "status": "success", "reward_value": 0.2, "reward_anchor": 0.8, "reward_direction": "higher_is_better"},
]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _bash_trace_write(db, payload_json):
    subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && trace_write "$1"', "_", payload_json],
        env={**os.environ, "MINI_ORK_DB": db}, capture_output=True, text=True, check=True,
    )


def _reward_g(db, trace_id):
    con = sqlite3.connect(db)
    r = con.execute("SELECT reward_g FROM execution_traces WHERE trace_id=?",
                    (trace_id,)).fetchone()
    con.close()
    return r[0] if r else "MISSING"


def test_compute_reward_g_unit():
    assert trace_store.compute_reward_g(1.0, 0.5, "higher_is_better") == 1.0
    assert trace_store.compute_reward_g(0.0, 0.5, "higher_is_better") == -1.0
    assert trace_store.compute_reward_g(1.0, 2.0, "lower_is_better") == 0.5
    assert trace_store.compute_reward_g(1.0, 0.0, "higher_is_better") is None
    assert trace_store.compute_reward_g(None, 0.5, "higher_is_better") is None


def test_reward_g_parity_bash_vs_python(db):
    for i, base in enumerate(PAYLOADS):
        bash_p = {**base, "trace_id": f"bash-{i}"}
        py_p = {**base, "trace_id": f"py-{i}"}
        _bash_trace_write(db, json.dumps(bash_p))
        trace_store.trace_write(py_p, db=db)
        bg = _reward_g(db, f"bash-{i}")
        pg = _reward_g(db, f"py-{i}")
        if bg is None or pg is None:
            assert bg == pg, f"payload {i}: bash={bg} py={pg}"
        else:
            assert abs(float(bg) - float(pg)) < 1e-9, f"payload {i}: bash={bg} py={pg}"


def test_roundtrip_get(db):
    tid = trace_store.trace_write(
        {"trace_id": "rt-1", "task_class": "code-fix", "status": "success",
         "reward_value": 1.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
        db=db)
    row = trace_store.trace_get(tid, db=db)
    assert row and row["status"] == "success" and abs(float(row["reward_g"]) - 1.0) < 1e-9


def test_objective_domain_passthrough(db):
    # objective_domain must land from the payload, not silently default to
    # code-delivery — the scoping-stamp fix (feature-partition column population).
    tid = trace_store.trace_write(
        {"trace_id": "od-1", "task_class": "book-gen", "status": "success",
         "objective_domain": "book-gen"}, db=db)
    row = trace_store.trace_get(tid, db=db)
    assert row["objective_domain"] == "book-gen"
    # unset → legacy code-delivery fallback preserved
    tid2 = trace_store.trace_write(
        {"trace_id": "od-2", "task_class": "x", "status": "success"}, db=db)
    assert trace_store.trace_get(tid2, db=db)["objective_domain"] == "code-delivery"


def _bash_grade(db, run_dir, run_id):
    subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && mo_grade_run_reward "$1" "$2"', "_", run_dir, run_id],
        env={**os.environ, "MINI_ORK_DB": db}, capture_output=True, text=True, check=True,
    )


def test_grade_run_reward_bash_vs_python(db, tmp_path):
    # Win #3 graded bridge: rubric.json {score 0-8} → reward_g in [-1,+1] stamped on
    # every trace of the run, overwriting the binary status-map reward. Assert the
    # bash mo_grade_run_reward and python grade_run_reward agree on the graded value.
    rd = tmp_path / "grade-run"; rd.mkdir()
    (rd / "rubric.json").write_text(json.dumps({"score": 6}))
    # seed one trace per side under distinct run_ids, initial reward_g = -1 (status-map fail)
    for tid, rid in (("gb-1", "grade-bash"), ("gp-1", "grade-py")):
        trace_store.trace_write(
            {"trace_id": tid, "run_id": rid, "task_class": "code-fix", "status": "failure",
             "reward_value": 0.0, "reward_anchor": 0.5, "reward_direction": "higher_is_better"},
            db=db)
    _bash_grade(db, str(rd), "grade-bash")
    n = trace_store.grade_run_reward(str(rd), "grade-py", db=db)
    assert n == 1
    bg, pg = _reward_g(db, "gb-1"), _reward_g(db, "gp-1")
    graded = (6 / 8 - 0.5) / 0.5   # 0.5
    assert abs(float(bg) - graded) < 1e-9 and abs(float(pg) - graded) < 1e-9
    # missing rubric → no-op (0 rows), leaves status-map reward intact
    empty = tmp_path / "no-rubric"; empty.mkdir()
    assert trace_store.grade_run_reward(str(empty), "grade-py", db=db) == 0
