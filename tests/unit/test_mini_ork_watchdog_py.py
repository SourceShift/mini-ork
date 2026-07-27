"""Unit tests: mini_ork.orchestration.watchdog (bash parity halves removed; formerly vs bin/mini-ork-watchdog).

Each test seeds a temp DB via ``db/init.sh``, invokes ``pass_once``, and
asserts the summary dict, the ``watchdog_aborts`` rows, and the
``<home>/runs/run-<id>/.stop-requested`` file contract. Floats are
checked within 1e-6 — trivially satisfied because the port rounds to 4
decimals. No mocks.

Cases (8):
  (1) empty DB → 0 active runs, 0 decisions
  (2) single run, 1 trace → skipped (len<2), no decisions
  (3) failure-pattern match → action="abort" + .stop-requested + DB row
  (4) warn_only flag → action="warned_only" + DB row, NO file
  (5) dry_run flag → action="would_abort", NO file, NO DB row
  (6) sub-threshold match → action="no-match", NO file, NO DB row
  (7) fail_count boost pushes score >= threshold → action="abort"
  (8) multi-pattern run → best match wins (cosine similarity is monotonic)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.orchestration import watchdog as py

INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Tooling checks + fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def db_home(tmp_path):
    """Bootstrap a fresh DB + MINI_ORK_HOME via db/init.sh."""
    for tool in ("bash", "sqlite3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    return {"home": str(home), "db": dbp}


# ─────────────────────────────────────────────────────────────────────────────
# Seed helpers — raw sqlite3 against the production schema
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> int:
    return int(time.time())


def _seed_task_runs(db: str, rows: list[dict]) -> None:
    """rows: list of {id, task_class, status?, created_at?}.

    ``id`` must be numeric (int or numeric string) — ``execution_traces.run_id``
    is INTEGER affinity, so non-numeric IDs would coerce to 0 and match no
    trace rows (the test would pass vacuously).
    """
    con = sqlite3.connect(db)
    for r in rows:
        created_at = r.get("created_at")
        if created_at is None:
            created_at = _now()
        con.execute(
            """
            INSERT INTO task_runs
                (id, task_class, recipe, kickoff_path, workflow_version,
                 created_at, updated_at, status)
            VALUES (?, ?, NULL, '/tmp/test.kickoff.md', 'latest', ?, ?, ?)
            """,
            (
                r["id"], r.get("task_class", "code_fix"),
                created_at, created_at,
                r.get("status", "executing"),
            ),
        )
    con.commit()
    con.close()


def _seed_execution_traces(db: str, rows: list[dict]) -> None:
    """rows: list of {trace_id, run_id, task_class, status, reviewer_verdict?}."""
    con = sqlite3.connect(db)
    for r in rows:
        con.execute(
            """
            INSERT INTO execution_traces
                (trace_id, run_id, agent_version_id, task_class,
                 prompt_version_hash, context_bundle_hash, tool_calls,
                 files_read, files_written, verifier_output, reviewer_verdict,
                 cost_usd, duration_ms, final_artifact_ref, status, created_at)
            VALUES (?, ?, '', ?, '', '', '[]', '[]', '[]', '{}', ?, 0.0, 0,
                    NULL, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (
                r["trace_id"], r["run_id"],
                r.get("task_class", "code_fix"),
                r.get("reviewer_verdict"),
                r["status"],
            ),
        )
    con.commit()
    con.close()


def _seed_pattern_records(db: str, rows: list[dict]) -> None:
    """rows: list of {pattern_id, description, output_type?, frequency?}."""
    con = sqlite3.connect(db)
    for r in rows:
        con.execute(
            """
            INSERT INTO pattern_records
                (pattern_id, description, evidence_trace_ids, frequency,
                 first_seen, last_seen, output_type, status)
            VALUES (?, ?, '[]', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, 'observed')
            """,
            (
                r["pattern_id"], r["description"],
                r.get("frequency", 1),
                r.get("output_type", "best_practice_rule"),
            ),
        )
    con.commit()
    con.close()


def _read_abort_rows(db: str) -> list[dict]:
    """Return all watchdog_aborts rows ordered by (run_id, matched_pattern)."""
    con = sqlite3.connect(db)
    try:
        try:
            cur = con.execute(
                "SELECT run_id, task_class, matched_pattern, match_score, "
                "evidence, outcome FROM watchdog_aborts "
                "ORDER BY run_id, matched_pattern"
            )
        except sqlite3.OperationalError:
            return []
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def _stop_requested_path(home: str, run_id: str) -> Path:
    return Path(home) / "runs" / f"run-{run_id}" / ".stop-requested"


def _read_stop_requested(home: str, run_id: str) -> str | None:
    p = _stop_requested_path(home, run_id)
    return p.read_text() if p.exists() else None


def _run_py(db_home: dict, *, threshold: float,
            dry_run: bool = False, warn_only: bool = False) -> dict:
    """Run the Python port with the given knobs."""
    return py.pass_once(
        db_home["db"], threshold=threshold, dry_run=dry_run,
        warn_only=warn_only, mini_ork_home=db_home["home"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# (1) Empty DB — no active runs, 0 decisions.
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_db(db_home):
    obj = _run_py(db_home, threshold=0.65)
    assert obj["decisions"] == []
    assert _read_abort_rows(db_home["db"]) == []


# ─────────────────────────────────────────────────────────────────────────────
# (2) Single run, 1 trace — early-continue (len(traces) < 2), no decisions.
# ─────────────────────────────────────────────────────────────────────────────
def test_short_trace_skipped(db_home):
    _seed_task_runs(db_home["db"], [
        {"id": 101, "task_class": "code_fix"},
    ])
    _seed_execution_traces(db_home["db"], [
        {"trace_id": "t1", "run_id": 101, "task_class": "code_fix",
         "status": "failure"},
    ])
    obj = _run_py(db_home, threshold=0.65)
    assert obj["decisions"] == []
    assert _read_abort_rows(db_home["db"]) == []


# ─────────────────────────────────────────────────────────────────────────────
# (3) Failure-pattern match — strong similarity → action="abort" + DB row.
# ─────────────────────────────────────────────────────────────────────────────
def test_failure_pattern_abort(db_home):
    _seed_pattern_records(db_home["db"], [
        {"pattern_id": "P-fail-001",
         "description": "pattern: status=failure on code_fix verifier"},
    ])
    _seed_task_runs(db_home["db"], [
        {"id": 201, "task_class": "code_fix"},
    ])
    _seed_execution_traces(db_home["db"], [
        {"trace_id": "t1", "run_id": 201, "task_class": "code_fix",
         "status": "failure"},
        {"trace_id": "t2", "run_id": 201, "task_class": "code_fix",
         "status": "failure", "reviewer_verdict": "REQUEST_CHANGES"},
    ])

    py_obj = _run_py(db_home, threshold=0.5)
    assert py_obj["decisions"][0]["action"] == "abort"
    # .stop-requested file written (run-201 per the run_dir layout).
    assert _read_stop_requested(db_home["home"], "201") is not None
    # watchdog_aborts row recorded.
    rows = _read_abort_rows(db_home["db"])
    assert len(rows) == 1
    assert rows[0]["matched_pattern"] == "P-fail-001"
    assert rows[0]["outcome"] == "aborted"


# ─────────────────────────────────────────────────────────────────────────────
# (4) warn_only → action="warned_only", DB row, NO file.
# ─────────────────────────────────────────────────────────────────────────────
def test_warn_only(db_home):
    _seed_pattern_records(db_home["db"], [
        {"pattern_id": "P-fail-002",
         "description": "pattern: status=failure on code_fix"},
    ])
    _seed_task_runs(db_home["db"], [
        {"id": 301, "task_class": "code_fix"},
    ])
    _seed_execution_traces(db_home["db"], [
        {"trace_id": "t1", "run_id": 301, "task_class": "code_fix",
         "status": "failure"},
        {"trace_id": "t2", "run_id": 301, "task_class": "code_fix",
         "status": "failure"},
    ])

    py_obj = _run_py(db_home, threshold=0.5, warn_only=True)
    assert py_obj["decisions"][0]["action"] == "warned_only"
    # warn_only never writes the .stop-requested file.
    assert _read_stop_requested(db_home["home"], "301") is None
    rows = _read_abort_rows(db_home["db"])
    assert len(rows) == 1
    assert rows[0]["outcome"] == "warned_only"


# ─────────────────────────────────────────────────────────────────────────────
# (5) dry_run → action="would_abort", NO file, NO DB row.
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run(db_home):
    _seed_pattern_records(db_home["db"], [
        {"pattern_id": "P-fail-003",
         "description": "pattern: status=failure on code_fix"},
    ])
    _seed_task_runs(db_home["db"], [
        {"id": 401, "task_class": "code_fix"},
    ])
    _seed_execution_traces(db_home["db"], [
        {"trace_id": "t1", "run_id": 401, "task_class": "code_fix",
         "status": "failure"},
        {"trace_id": "t2", "run_id": 401, "task_class": "code_fix",
         "status": "failure"},
    ])

    py_obj = _run_py(db_home, threshold=0.5, dry_run=True)
    assert py_obj["decisions"][0]["action"] == "would_abort"
    # dry_run never writes .stop-requested AND never inserts watchdog_aborts.
    assert _read_stop_requested(db_home["home"], "401") is None
    assert _read_abort_rows(db_home["db"]) == []


# ─────────────────────────────────────────────────────────────────────────────
# (6) Sub-threshold match → action="no-match", NO file, NO DB row.
# ─────────────────────────────────────────────────────────────────────────────
def test_subthreshold_no_match(db_home):
    _seed_pattern_records(db_home["db"], [
        {"pattern_id": "P-vacuous-006",
         "description": "pattern: status=vacuous on different task_class"},
    ])
    _seed_task_runs(db_home["db"], [
        {"id": 501, "task_class": "research_synthesis"},
    ])
    # Traces don't share enough tokens with the pattern → low similarity.
    _seed_execution_traces(db_home["db"], [
        {"trace_id": "t1", "run_id": 501,
         "task_class": "research_synthesis", "status": "success"},
        {"trace_id": "t2", "run_id": 501,
         "task_class": "research_synthesis", "status": "success"},
    ])

    py_obj = _run_py(db_home, threshold=0.65)
    # Score below threshold → action stays at the default "no-match".
    assert len(py_obj["decisions"]) == 1
    assert py_obj["decisions"][0]["action"] == "no-match"
    assert py_obj["decisions"][0]["match_score"] < 0.65
    assert _read_stop_requested(db_home["home"], "501") is None
    assert _read_abort_rows(db_home["db"]) == []


# ─────────────────────────────────────────────────────────────────────────────
# (7) fail_count boost pushes score over threshold.
# ─────────────────────────────────────────────────────────────────────────────
def test_fail_count_boost(db_home):
    _seed_pattern_records(db_home["db"], [
        {"pattern_id": "P-fail-007",
         "description": "pattern: status=failure on code_fix"},
    ])
    _seed_task_runs(db_home["db"], [
        {"id": 601, "task_class": "code_fix"},
    ])
    # 4 failure traces → fail_boost = min(0.20, 0.05*4) = 0.20, which pushes
    # even a modest similarity over the 0.65 default threshold.
    _seed_execution_traces(db_home["db"], [
        {"trace_id": "t1", "run_id": 601, "task_class": "code_fix",
         "status": "failure"},
        {"trace_id": "t2", "run_id": 601, "task_class": "code_fix",
         "status": "failure"},
        {"trace_id": "t3", "run_id": 601, "task_class": "code_fix",
         "status": "failure"},
        {"trace_id": "t4", "run_id": 601, "task_class": "code_fix",
         "status": "failure"},
    ])

    py_obj = _run_py(db_home, threshold=0.65)
    assert py_obj["decisions"][0]["action"] == "abort"
    # The fail_count is exactly 4 (4 failure traces).
    assert py_obj["decisions"][0]["fail_count"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# (8) Multi-pattern run — best (highest cosine) match wins.
# ─────────────────────────────────────────────────────────────────────────────
def test_multi_pattern_best_match(db_home):
    _seed_pattern_records(db_home["db"], [
        {"pattern_id": "P-loose",
         "description": "unrelated pattern about task_class=documentation"},
        {"pattern_id": "P-tight",
         "description": "pattern: status=failure on code_fix tight match"},
    ])
    _seed_task_runs(db_home["db"], [
        {"id": 701, "task_class": "code_fix"},
    ])
    _seed_execution_traces(db_home["db"], [
        {"trace_id": "t1", "run_id": 701, "task_class": "code_fix",
         "status": "failure"},
        {"trace_id": "t2", "run_id": 701, "task_class": "code_fix",
         "status": "failure"},
    ])

    py_obj = _run_py(db_home, threshold=0.5)
    # The tight pattern wins, not the loose one.
    assert py_obj["decisions"][0]["matched_pattern"] == "P-tight"
