"""Parity gate: mini_ork.learning.process_reward.score_trace vs bash prm_score_trace.

For each fixture, we seed a fresh temp sqlite DB with the same row shape
``lib/process_reward.sh::prm_score_trace`` consumes, invoke the live bash
function via subprocess (no mocking), and assert ``|bash - python| < 1e-6``.

The bash function reads JSON-as-TEXT from ``execution_traces`` and writes
the score back to ``process_reward``. The Python port must therefore accept
JSON strings for ``tool_calls`` / ``files_written`` / ``files_read`` —
exactly what the row contains.

Strangler-fig co-existence is preserved: ``lib/process_reward.sh`` is
byte-identical before and after this test file exists.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mini_ork.learning.process_reward import score_trace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIB_PROCESS_REWARD = REPO_ROOT / "lib" / "process_reward.sh"

EXEC_TRACES_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_traces (
    trace_id           TEXT PRIMARY KEY,
    status             TEXT,
    tool_calls         TEXT NOT NULL DEFAULT '[]',
    files_written      TEXT NOT NULL DEFAULT '[]',
    files_read         TEXT NOT NULL DEFAULT '[]',
    reviewer_verdict   TEXT,
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0.0,
    process_reward     REAL
);
"""


def _seed_db(db_path: Path, trace_id: str, fixture: dict) -> None:
    con = sqlite3.connect(str(db_path))
    con.executescript(EXEC_TRACES_SCHEMA)
    con.execute(
        "INSERT INTO execution_traces ("
        "trace_id, status, tool_calls, files_written, files_read,"
        "reviewer_verdict, duration_ms, cost_usd"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trace_id,
            fixture.get("status"),
            fixture.get("tool_calls", "[]"),
            fixture.get("files_written", "[]"),
            fixture.get("files_read", "[]"),
            fixture.get("reviewer_verdict"),
            fixture.get("duration_ms", 0),
            fixture.get("cost_usd", 0.0),
        ),
    )
    con.commit()
    con.close()


def _run_bash_prm(trace_id: str, db_path: Path) -> float:
    env = os.environ.copy()
    env["MINI_ORK_ROOT"] = str(REPO_ROOT)
    env["MO_STORE_DB"] = str(db_path)
    env["MO_STORE_BACKEND"] = "sqlite"
    env.pop("MO_PRM_ACTIVITY_CAP", None)
    proc = subprocess.run(
        ["bash", "-c", f'. "{LIB_PROCESS_REWARD}" && prm_score_trace "{trace_id}"'],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return float(proc.stdout.strip())


def _parity(fixture: dict, tmp_path: Path, trace_id: str = "tr-fixture") -> tuple[float, float]:
    db_path = tmp_path / f"{trace_id}.sqlite"
    _seed_db(db_path, trace_id, fixture)
    bash_score = _run_bash_prm(trace_id, db_path)
    py_score = score_trace(fixture)
    return bash_score, py_score


# Fixture set — covers the trigger matrix of the weight table plus the
# Goodhart activity cap and verdict gating. Fields omitted from a fixture
# default to the empty/zero SQLite representation, which matches what
# bash sees when those columns are NULL/0.
FIXTURES = {
    "bare_success": {"status": "success"},
    "bare_failed": {"status": "failure"},
    "failed_heavy_activity_capped": {
        "status": "failure",
        "tool_calls": json.dumps([{"name": "bash"}, {"name": "edit"}, {"name": "read"}]),
        "files_written": json.dumps(["a.py", "b.py"]),
        "files_read": json.dumps(["c.py"]),
        "cost_usd": 0.05,
        "duration_ms": 5000,
        # Bash: 0 + min(0.20+0.10, 0.15) + 0 + 0.10 + 0.05 = 0.30
    },
    "success_verdict_approve": {
        "status": "success",
        "reviewer_verdict": "approve",
        "duration_ms": 3000,
        # 0.40 + 0 + 0.15 + 0.10 = 0.65
    },
    "success_verdict_fail": {
        "status": "success",
        "reviewer_verdict": "reject",
        "duration_ms": 3000,
        # 0.40 + 0 + 0 + 0.10 = 0.50
    },
    "failed_verdict_approve_gated": {
        "status": "failure",
        "reviewer_verdict": "approve",
        "duration_ms": 3000,
        # verdict gated: 0.10 only
    },
    "duration_below_floor_999ms": {
        "status": "success",
        "duration_ms": 999,
        # 0.40 only — too fast
    },
    "duration_at_floor_1000ms": {
        "status": "success",
        "duration_ms": 1000,
        # 0.40 + 0.10 = 0.50
    },
    "duration_at_ceiling_600000ms": {
        "status": "success",
        "duration_ms": 600000,
        # 0.40 + 0.10 = 0.50
    },
    "duration_above_ceiling_600001ms": {
        "status": "success",
        "duration_ms": 600001,
        # 0.40 only — too slow
    },
    "cost_zero_no_bonus": {
        "status": "success",
        "cost_usd": 0.0,
        "duration_ms": 3000,
        # 0.40 + 0.10 = 0.50
    },
    "cost_positive_bonus": {
        "status": "success",
        "cost_usd": 0.001,
        "duration_ms": 3000,
        # 0.40 + 0.10 + 0.05 = 0.55
    },
    "empty_none_fields": {
        # status defaults to "" via get fallback → no status_success
        # tool_calls/files default "[]" via .get(..., "[]") in helper
        # reviewer_verdict None → ""
        "duration_ms": 0,
    },
    "tool_plus_file_activity_under_cap": {
        "status": "success",
        "tool_calls": json.dumps([{"name": "bash"}]),
        "files_written": json.dumps(["x.py"]),
        "duration_ms": 3000,
        # 0.40 + min(0.20+0.10, 0.15) + 0.10 = 0.65 — verifies cap=0.15 not 0.30
    },
}


@pytest.mark.parametrize("fixture", list(FIXTURES.values()), ids=list(FIXTURES.keys()))
def test_score_trace_matches_bash_prm(fixture, tmp_path):
    bash_score, py_score = _parity(fixture, tmp_path)
    assert math.isclose(bash_score, py_score, abs_tol=1e-6), (
        f"parity drift: bash={bash_score!r} py={py_score!r} "
        f"fixture={fixture!r}"
    )


def test_smoke_import_and_score():
    """Importing the module and scoring a minimal fixture returns a float in [0, 1]."""
    score = score_trace({"status": "success"})
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0