"""Eval-loop closure (win #3): the rubric's graded 0-8 run score must reach the
learning reward as a graded reward_g, not a flattened pass/fail. Verifies the
grading curve of the native ``trace_store.grade_run_reward``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import trace_store  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
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
    assert abs(_reward_g(db, f"{run_id}-1") - expected) < 1e-9


def test_grade_only_touches_target_run(db, tmp_path):
    rd = tmp_path / "rd"
    rd.mkdir()
    (rd / "rubric.json").write_text(json.dumps({"pass": True, "score": 6, "items": []}))
    _seed(db, "run-target")
    _seed(db, "run-other")
    n = trace_store.grade_run_reward(str(rd), "run-target", db=db)
    assert n == 2
    assert _reward_g(db, "run-target-0") == 0.5
    # the other run's traces keep their status-map reward (NULL here: no
    # reward_value/anchor seeded)
    assert _reward_g(db, "run-other-0") is None
