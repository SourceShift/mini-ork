"""close_run_record and the human-review park sentinel.

A parked run (executor set ``reviewing`` and wrote ``.review-pause``) keeps its
status at lifecycle teardown. A run that was resumed past the gate but still
carries a stale sentinel must NOT be exempted: the live smoke of 2026-10-15
(hr-smoke-b-152611, resumed by a hand-run ``recover``) finished every node and
was left ``executing`` because the exemption keyed on the file alone.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.orchestration import run_reaper
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, run_id: str, status: str) -> Path:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, task_class, "
        "kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", status, 0.0, now - 60, now, "code_fix", "", "latest"))
    con.commit()
    con.close()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / ".review-pause").write_text(
        json.dumps({"waiting_node": "typecheck", "awaiting": ["implementer"]}), encoding="utf-8")
    return run_dir


def _status(home: Path, run_id: str) -> str:
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute("SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    finally:
        con.close()


def test_a_parked_reviewing_run_keeps_its_status(home: Path) -> None:
    run_dir = _seed(home, "run-parked", "reviewing")
    assert run_reaper.close_run_record(home / "state.db", "run-parked", run_dir,
                                       crashed=False, rc=9) is None
    assert _status(home, "run-parked") == "reviewing"


def test_a_stale_sentinel_does_not_strand_a_resumed_run(home: Path) -> None:
    run_dir = _seed(home, "run-resumed", "executing")
    assert run_reaper.close_run_record(home / "state.db", "run-resumed", run_dir,
                                       crashed=False, rc=1) == "executing"
    assert _status(home, "run-resumed") == "failed"
