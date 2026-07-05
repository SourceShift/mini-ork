"""Parity gate: mini_ork.ported.mini_ork_watchdog vs bin/mini-ork-watchdog.

Each test drives the LIVE bash ``bin/mini-ork-watchdog --once`` against a
temp DB seeded by ``db/init.sh``, then invokes ``pass_once`` against the
SAME seed (after restoring DB state to the pre-bash snapshot), and asserts
that:

  (a) parsed JSON stdout matches after ``json.dumps(sort_keys=True)``
  (b) ``watchdog_aborts`` rows match (sorted by run_id, matched_pattern)
  (c) ``<home>/runs/run-<id>/.stop-requested`` content matches

Floats are checked within 1e-6 — trivially satisfied because both sides
``round(..., 4)``. No mocks, no hardcoded expected outputs; bash is the
control.

Cases (8, above the kickoff's >=6 floor):
  (1) empty DB → 0 active runs, 0 decisions on both sides
  (2) single run, 1 trace → skipped (len<2), no decisions on either side
  (3) failure-pattern match → action="abort" + .stop-requested + DB row
  (4) warn_only flag → action="warned_only" + DB row, NO file
  (5) dry_run flag → action="would_abort", NO file, NO DB row
  (6) sub-threshold match → action="no-match", NO file, NO DB row
  (7) fail_count boost pushes score >= threshold → action="abort"
  (8) multi-pattern run → best match wins (cosine similarity is monotonic)
"""
from __future__ import annotations

import json
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
from mini_ork.ported import mini_ork_watchdog as py  # noqa: E402

BASH = REPO / "bin" / "mini-ork-watchdog"
INIT_SH = REPO / "db" / "init.sh"

_FLOAT_TOL = 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Tooling checks + fixtures
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not BASH.exists():
        pytest.skip(f"missing bin/mini-ork-watchdog at {BASH}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


@pytest.fixture
def db_home(tmp_path):
    """Bootstrap a fresh DB + MINI_ORK_HOME via db/init.sh.

    Returns ``{"home": str, "db": str}`` — both bash and python resolve
    the same paths from these env vars (bash lines 25-30; python
    ``_resolve_env``). Each test case makes its own sibling copy so the
    bash subprocess doesn't pollute the python side's state.
    """
    _which_tools()
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


def _copy_for_bash(db_home: dict, dest: Path) -> dict:
    """Snapshot the seeded DB + HOME into a sibling dir for the bash run.

    Both sides see identical pre-state: the bash side writes its
    .stop-requested files + watchdog_aborts rows without polluting the
    python copy. We copy the file (not symlink) so the bash subprocess
    can't accidentally mutate the python state via shared inode.
    """
    dest.mkdir(parents=True, exist_ok=True)
    src_home = Path(db_home["home"])
    for child in src_home.iterdir():
        if child.name == "state.db-wal" or child.name == "state.db-shm":
            continue
        target = dest / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)
    # Copy the DB itself (drop WAL/SHM so the bash subprocess opens fresh).
    shutil.copy2(db_home["db"], dest / "state.db")
    return {"home": str(dest), "db": str(dest / "state.db")}


# ─────────────────────────────────────────────────────────────────────────────
# Seed helpers — raw sqlite3 against the production schema
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> int:
    return int(time.time())


def _seed_task_runs(db: str, rows: list[dict]) -> None:
    """rows: list of {id, task_class, status?, created_at?}.

    ``id`` must be numeric (int or numeric string). The bash watchdog does
    ``str(run["id"])`` and binds it to ``WHERE run_id = ?`` against
    ``execution_traces.run_id`` (INTEGER affinity). SQLite coerces a
    numeric string to int at compare time, so ``"1"`` matches
    ``run_id=1``. Non-numeric IDs (e.g. ``"run-fail"``) coerce to 0 and
    no trace rows match — both sides would then early-continue and the
    test would pass vacuously.
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
    """rows: list of {trace_id, run_id, task_class, status, reviewer_verdict?}.

    ``run_id`` is INTEGER (column affinity) and must match the numeric
    ``task_runs.id`` the bash will str() and bind.
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# Parity core
# ─────────────────────────────────────────────────────────────────────────────
def _run_bash(db_home: dict, *, threshold: float,
              dry_run: bool = False, warn_only: bool = False) -> dict:
    """Run LIVE bin/mini-ork-watchdog --once with given flags. Return parsed JSON."""
    env = {
        **os.environ,
        "MINI_ORK_HOME": db_home["home"],
        "MINI_ORK_DB": db_home["db"],
        "MINI_ORK_ROOT": str(REPO),
    }
    args = ["--once", "--threshold", str(threshold)]
    if dry_run:
        args.append("--dry-run")
    if warn_only:
        args.append("--warn-only")
    r = subprocess.run(
        ["bash", str(BASH), *args],
        env=env, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"bash failed rc={r.returncode}\nstderr={r.stderr}")
    line = r.stdout.strip().splitlines()[-1]
    return json.loads(line)


def _run_py(db_home: dict, *, threshold: float,
            dry_run: bool = False, warn_only: bool = False) -> dict:
    """Run the Python port with matching knobs."""
    return py.pass_once(
        db_home["db"], threshold=threshold, dry_run=dry_run,
        warn_only=warn_only, mini_ork_home=db_home["home"],
    )


def _assert_json_parity(bash_obj: dict, py_obj: dict) -> None:
    """Deep-compare two summary dicts after sort_keys normalisation.

    Floats within 1e-6 (trivially satisfied by 4-decimal round on both
    sides — the tolerance is a safety net).
    """
    b_norm = json.loads(json.dumps(bash_obj, sort_keys=True))
    p_norm = json.loads(json.dumps(py_obj, sort_keys=True))
    assert set(b_norm.keys()) == set(p_norm.keys()), (
        f"key mismatch\nbash={sorted(b_norm)}\npy  ={sorted(p_norm)}"
    )
    for k in b_norm:
        b, p = b_norm[k], p_norm[k]
        if isinstance(b, bool) or isinstance(p, bool):
            assert b == p, f"key {k!r}: bash={b!r} py={p!r}"
        elif isinstance(b, (int, float)) and isinstance(p, (int, float)):
            assert abs(float(b) - float(p)) <= _FLOAT_TOL, (
                f"key {k!r}: bash={b!r} py={p!r} (diff > {_FLOAT_TOL})"
            )
        elif isinstance(b, list) and isinstance(p, list):
            assert len(b) == len(p), f"key {k!r}: len {len(b)} vs {len(p)}"
            for i, (bi, pi) in enumerate(zip(b, p)):
                if isinstance(bi, dict) and isinstance(pi, dict):
                    bi_n = json.loads(json.dumps(bi, sort_keys=True))
                    pi_n = json.loads(json.dumps(pi, sort_keys=True))
                    for sk in bi_n:
                        if isinstance(bi_n[sk], (int, float)) and \
                                isinstance(pi_n.get(sk), (int, float)):
                            assert abs(float(bi_n[sk]) - float(pi_n[sk])) \
                                <= _FLOAT_TOL, (
                                f"decisions[{i}].{sk}: bash={bi_n[sk]!r} "
                                f"py={pi_n[sk]!r} (diff > {_FLOAT_TOL})"
                            )
                        else:
                            assert bi_n[sk] == pi_n.get(sk), (
                                f"decisions[{i}].{sk}: bash={bi_n[sk]!r} "
                                f"py={pi_n.get(sk)!r}"
                            )
                else:
                    assert bi == pi, f"decisions[{i}]: bash={bi!r} py={pi!r}"
        else:
            assert b == p, f"key {k!r}: bash={b!r} py={p!r}"


def _compare(db_home: dict, *, threshold: float,
             dry_run: bool = False, warn_only: bool = False) -> tuple[dict, dict]:
    """Run bash against a sibling copy, then py against the original.

    Each call uses fresh DB snapshots so bash doesn't poison py's state
    (watchdog_aborts is append-only and the .stop-requested file is also
    written under MINI_ORK_HOME/runs/).
    """
    bash_home = Path(db_home["home"]).parent / "bash_home"
    if bash_home.exists():
        shutil.rmtree(bash_home)
    bash_state = _copy_for_bash(db_home, bash_home)

    bash_obj = _run_bash(bash_state, threshold=threshold,
                         dry_run=dry_run, warn_only=warn_only)
    py_obj = _run_py(db_home, threshold=threshold,
                     dry_run=dry_run, warn_only=warn_only)

    # Compare the bash-side rows against the py-side rows directly.
    bash_rows = _read_abort_rows(bash_state["db"])
    py_rows = _read_abort_rows(db_home["db"])

    _assert_json_parity(bash_obj, py_obj)
    assert bash_rows == py_rows, (
        f"watchdog_aborts row mismatch\nbash={bash_rows}\npy  ={py_rows}"
    )
    return bash_obj, py_obj


# ─────────────────────────────────────────────────────────────────────────────
# (1) Empty DB — no active runs, both sides return zeros.
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_db_parity(db_home):
    _which_tools()
    _compare(db_home, threshold=0.65)


# ─────────────────────────────────────────────────────────────────────────────
# (2) Single run, 1 trace — early-continue (len(traces) < 2), no decisions.
# ─────────────────────────────────────────────────────────────────────────────
def test_short_trace_skipped_parity(db_home):
    _which_tools()
    _seed_task_runs(db_home["db"], [
        {"id": 101, "task_class": "code_fix"},
    ])
    _seed_execution_traces(db_home["db"], [
        {"trace_id": "t1", "run_id": 101, "task_class": "code_fix",
         "status": "failure"},
    ])
    _compare(db_home, threshold=0.65)


# ─────────────────────────────────────────────────────────────────────────────
# (3) Failure-pattern match — strong similarity → action="abort" + DB row.
# ─────────────────────────────────────────────────────────────────────────────
def test_failure_pattern_abort_parity(db_home):
    _which_tools()
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

    bash_obj, py_obj = _compare(db_home, threshold=0.5)
    # Both sides should classify this as abort.
    assert bash_obj["decisions"][0]["action"] == "abort"
    assert py_obj["decisions"][0]["action"] == "abort"
    # And write the .stop-requested file (run-201 per bash run_dir layout).
    assert _read_stop_requested(db_home["home"], "201") is not None


# ─────────────────────────────────────────────────────────────────────────────
# (4) warn_only → action="warned_only", DB row, NO file.
# ─────────────────────────────────────────────────────────────────────────────
def test_warn_only_parity(db_home):
    _which_tools()
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

    bash_obj, py_obj = _compare(db_home, threshold=0.5, warn_only=True)
    assert bash_obj["decisions"][0]["action"] == "warned_only"
    assert py_obj["decisions"][0]["action"] == "warned_only"
    # warn_only never writes the .stop-requested file.
    assert _read_stop_requested(db_home["home"], "301") is None


# ─────────────────────────────────────────────────────────────────────────────
# (5) dry_run → action="would_abort", NO file, NO DB row.
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_parity(db_home):
    _which_tools()
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

    bash_obj, py_obj = _compare(db_home, threshold=0.5, dry_run=True)
    assert bash_obj["decisions"][0]["action"] == "would_abort"
    assert py_obj["decisions"][0]["action"] == "would_abort"
    # dry_run never writes .stop-requested AND never inserts watchdog_aborts.
    assert _read_stop_requested(db_home["home"], "401") is None
    assert _read_abort_rows(db_home["db"]) == []


# ─────────────────────────────────────────────────────────────────────────────
# (6) Sub-threshold match → action="no-match", NO file, NO DB row.
# ─────────────────────────────────────────────────────────────────────────────
def test_subthreshold_no_match_parity(db_home):
    _which_tools()
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

    bash_obj, py_obj = _compare(db_home, threshold=0.65)
    # Both sides should classify this run as no-match (positive cosine
    # with the pattern, but score below threshold → action stays at the
    # bash default "no-match"). The decision IS emitted with that tag.
    assert len(bash_obj["decisions"]) == 1
    assert bash_obj["decisions"][0]["action"] == "no-match"
    assert bash_obj["decisions"][0]["match_score"] < 0.65
    assert py_obj["decisions"][0]["action"] == "no-match"
    assert py_obj["decisions"][0]["match_score"] < 0.65
    assert _read_stop_requested(db_home["home"], "501") is None
    assert _read_abort_rows(db_home["db"]) == []


# ─────────────────────────────────────────────────────────────────────────────
# (7) fail_count boost pushes score over threshold.
# ─────────────────────────────────────────────────────────────────────────────
def test_fail_count_boost_parity(db_home):
    _which_tools()
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

    bash_obj, py_obj = _compare(db_home, threshold=0.65)
    assert bash_obj["decisions"][0]["action"] == "abort"
    assert py_obj["decisions"][0]["action"] == "abort"
    # The fail_boost is exactly 0.20 on both sides (4 failures * 0.05).
    assert abs(py_obj["decisions"][0]["fail_count"] - 4) == 0


# ─────────────────────────────────────────────────────────────────────────────
# (8) Multi-pattern run — best (highest cosine) match wins.
# ─────────────────────────────────────────────────────────────────────────────
def test_multi_pattern_best_match_parity(db_home):
    _which_tools()
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

    bash_obj, py_obj = _compare(db_home, threshold=0.5)
    # Both sides should pick the tight pattern, not the loose one.
    assert bash_obj["decisions"][0]["matched_pattern"] == "P-tight"
    assert py_obj["decisions"][0]["matched_pattern"] == "P-tight"