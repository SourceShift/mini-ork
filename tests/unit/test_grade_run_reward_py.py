"""Eval-loop closure (win #3): the rubric's graded 0-8 run score must reach the
learning reward as a graded reward_g, not a flattened pass/fail. Verifies the
grading curve and bash↔python parity of mo_grade_run_reward.
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


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    return dbp


def _seed(db, run_id, n=2):
    for i in range(n):
        trace_store.trace_write(
            {"trace_id": f"{run_id}-{i}", "run_id": run_id, "task_class": "code-fix",
             "status": "success", "agent_version_id": "minimax"}, db=db)


def _reward_g(db, trace_id):
    con = sqlite3.connect(db)
    r = con.execute("SELECT reward_g FROM execution_traces WHERE trace_id=?",
                    (trace_id,)).fetchone()
    con.close()
    return r[0] if r else None


@pytest.mark.parametrize("score,expected", [(0, -1.0), (4, 0.0), (6, 0.5), (8, 1.0)])
def test_graded_curve_python(db, tmp_path, score, expected):
    run_id = f"run-{score}"
    _seed(db, run_id)
    rd = tmp_path / f"rd{score}"
    rd.mkdir()
    (rd / "rubric.json").write_text(json.dumps({"pass": True, "score": score, "items": []}))
    n = trace_store.grade_run_reward(str(rd), run_id, db=db)
    assert n == 2  # both traces of the run graded
    assert abs(_reward_g(db, f"{run_id}-0") - expected) < 1e-9


def test_bash_python_parity(db, tmp_path):
    rd = tmp_path / "rd"
    rd.mkdir()
    (rd / "rubric.json").write_text(json.dumps({"pass": True, "score": 6, "items": []}))
    _seed(db, "run-py")
    _seed(db, "run-bash")
    trace_store.grade_run_reward(str(rd), "run-py", db=db)
    subprocess.run(
        ["bash", "-c", f'. "{TS_SH}" && mo_grade_run_reward "$1" "$2"', "_", str(rd), "run-bash"],
        env={**os.environ, "MINI_ORK_DB": db}, capture_output=True, text=True, check=True)
    assert _reward_g(db, "run-py-0") == _reward_g(db, "run-bash-0") == 0.5
