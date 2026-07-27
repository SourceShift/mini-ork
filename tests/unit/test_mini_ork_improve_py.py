"""Unit tests: ``mini_ork.cli.improve`` (bash parity halves removed; formerly vs ``bin/mini-ork-improve``).

Drives ``python3 -m mini_ork.cli.improve`` against a ``db/init.sh``-seeded
tmp DB and asserts the deterministic surface semantically. No mocks.

Cases:
  (1) --help long     — rc=0, usage on stdout, stderr empty.
  (2) -h short        — same as (1).
  (3) unknown flag    — rc=2, stderr contains 'Unknown flag' + the bad arg.
  (4) --dry-run       — seeded DB; perf_summary block parses to the expected
                          GROUP-BY rows.
  (5) --task-class    — dry-run with scope line; perf_summary narrowed to
                          ONE task_class only.
  (6) happy path      — seeded DB; '=== mini-ork improve ===', 'Proposed
                          candidates:', 3x 'candidate_id=wc-', trailing raw IDs.
  (7) DB-row check    — the port UPSERTs 1 execution_traces row with
                          task_class='__improve__' (running → success same
                          trace_id); count=1 + status='success'.
  (8) workflow_candidates rows   — the port writes N=limit=3 rows; ids
                          match /wc-[0-9a-f]+/.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

PY_MOD = "mini_ork.cli.improve"

CAND_ID_RE = re.compile(r"^wc-[0-9a-f]{6,}$")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _scenario(home: Path):
    """Seed a tmp DB at <home>/state.db via db/init.sh. Returns (home, db_str)."""
    home.mkdir(parents=True, exist_ok=True)
    db = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ,
             "MINI_ORK_HOME": str(home),
             "MINI_ORK_DB": db},
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert r.returncode == 0, f"db/init.sh failed (rc={r.returncode}): {r.stderr}"
    return home, db


def _seed_traces(db: str, rows):
    """Insert deterministic execution_traces rows.

    rows: list of (trace_id, task_class, status, duration_ms, cost_usd,
    created_iso). caller pins durations/costs so AVG columns are
    reproducible across runs.
    """
    con = sqlite3.connect(db)
    try:
        con.executemany(
            "INSERT INTO execution_traces "
            "(trace_id, task_class, status, duration_ms, cost_usd, "
            "created_at) VALUES (?,?,?,?,?,?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def _py(home: Path, db: str, args, *, extra_env=None):
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": db,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["python3", "-m", PY_MOD] + list(args),
        capture_output=True, text=True, env=env, cwd=str(REPO),
    )


def _perf_block(stdout: str) -> list:
    """Slice stdout to the perf_summary JSON block.

    Returns the lines between the ``[dry-run] performance summary:``
    header and the first following blank line.
    """
    lines = stdout.splitlines()
    out, in_block = [], False
    for ln in lines:
        if ln == "[dry-run] performance summary:":
            in_block = True
            continue
        if in_block and ln == "":
            break
        if in_block:
            out.append(ln)
    return out


def _perf_parsed(stdout: str) -> list:
    block = _perf_block(stdout)
    text = "\n".join(block)
    return json.loads(text) if text.strip() else []


# ─────────────────────────────────────────────────────────────────────────────
# Test data — pinned times + reproducibly-grouped perf_summary.
# ─────────────────────────────────────────────────────────────────────────────
TRACE_ROWS = [
    ("trace-seed-001", "code_fix",       "success", 5000, 0.123,
     "2026-01-15T12:00:00.000Z"),
    ("trace-seed-002", "code_fix",       "success", 7000, 0.456,
     "2026-01-15T12:01:00.000Z"),
    ("trace-seed-003", "refactor_audit", "success", 9000, 0.789,
     "2026-01-15T12:02:00.000Z"),
    ("trace-seed-004", "refactor_audit", "failure", 11000, 1.012,
     "2026-01-15T12:03:00.000Z"),
]


# ─────────────────────────────────────────────────────────────────────────────
# (1) --help
# ─────────────────────────────────────────────────────────────────────────────
def test_help_long_flag(tmp_path):
    home_py = tmp_path / "py"; home_py.mkdir()
    rp = _py(home_py, str(home_py / "state.db"), ["--help"])
    assert rp.returncode == 0
    assert "Usage: mini-ork improve" in rp.stdout
    assert "--task-class" in rp.stdout
    assert "--limit" in rp.stdout
    assert "--dry-run" in rp.stdout
    assert rp.stderr == ""


# ─────────────────────────────────────────────────────────────────────────────
# (2) -h short
# ─────────────────────────────────────────────────────────────────────────────
def test_help_short_flag(tmp_path):
    home_py = tmp_path / "py"; home_py.mkdir()
    rp = _py(home_py, str(home_py / "state.db"), ["-h"])
    assert rp.returncode == 0
    assert "Usage: mini-ork improve" in rp.stdout
    assert rp.stderr == ""


# ─────────────────────────────────────────────────────────────────────────────
# (3) unknown flag -> rc=2 + stderr 'Unknown flag'
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_flag_exits_2(tmp_path):
    home_py = tmp_path / "py"; home_py.mkdir()
    rp = _py(home_py, str(home_py / "state.db"), ["--nope"])
    assert rp.returncode == 2
    assert "Unknown flag" in rp.stderr
    # The offending flag is echoed back.
    assert "--nope" in rp.stderr


# ─────────────────────────────────────────────────────────────────────────────
# (4) --dry-run against a seeded DB
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_against_seeded_db(tmp_path):
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_py, TRACE_ROWS)

    rp = _py(home_py, db_py, ["--dry-run"])

    assert rp.returncode == 0
    assert "[dry-run] performance summary:" in rp.stdout
    assert "[dry-run] would call group_evolver with limit=3" in rp.stdout
    assert rp.stderr == ""

    # perf_summary parses to the GROUP-BY rows over the seed.
    parsed = _perf_parsed(rp.stdout)
    assert len(parsed) == 2
    for p in parsed:
        assert set(p.keys()) == {
            "task_class", "total_runs", "successes",
            "avg_duration_ms", "avg_cost_usd",
        }
    by_class = {p["task_class"]: p for p in parsed}
    cf = by_class["code_fix"]
    assert cf["total_runs"] == 2 and cf["successes"] == 2
    assert abs(cf["avg_duration_ms"] - 6000) < 1e-6
    assert abs(cf["avg_cost_usd"] - (0.123 + 0.456) / 2) < 1e-6
    ra = by_class["refactor_audit"]
    assert ra["total_runs"] == 2 and ra["successes"] == 1
    assert abs(ra["avg_duration_ms"] - 10000) < 1e-6
    assert abs(ra["avg_cost_usd"] - (0.789 + 1.012) / 2) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (5) --dry-run + --task-class narrows perf_summary to one class
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_with_task_class_filter(tmp_path):
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_py, TRACE_ROWS)

    rp = _py(home_py, db_py,
             ["--dry-run", "--task-class", "code_fix"])

    assert rp.returncode == 0
    assert "[dry-run] scope: task_class=code_fix" in rp.stdout

    parsed = _perf_parsed(rp.stdout)
    # the WHERE clause narrows to one task_class
    assert len(parsed) == 1
    assert parsed[0]["task_class"] == "code_fix"
    # counts reflect code_fix's 2 seed rows
    assert parsed[0]["total_runs"] == 2
    assert parsed[0]["successes"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# (6) happy path: full dispatch
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_proposes_and_stores_candidates(tmp_path):
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_py, TRACE_ROWS)

    rp = _py(home_py, db_py, [])

    assert rp.returncode == 0
    assert "=== mini-ork improve ===" in rp.stdout
    assert "Proposed candidates:" in rp.stdout
    assert "Persisted 3 candidate(s) to workflow_candidates table." in rp.stdout
    assert rp.stdout.count("candidate_id=wc-") == 3

    # Last N lines (== candidate_limit == 3) are raw IDs.
    py_lines = rp.stdout.splitlines()
    for line in py_lines[-3:]:
        assert CAND_ID_RE.match(line), f"bad id line: {line!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (7) execution_traces row check after non-dry-run
# ─────────────────────────────────────────────────────────────────────────────
def test_improve_exec_traces_row(tmp_path):
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_py, TRACE_ROWS)

    rp = _py(home_py, db_py, [])
    assert rp.returncode == 0

    # The port UPSERTs (running → success same trace_id) so the final state
    # is exactly 1 row with status='success'.
    con = sqlite3.connect(db_py)
    try:
        rows = con.execute(
            "SELECT trace_id, task_class, status, created_at "
            "FROM execution_traces WHERE task_class='__improve__' "
            "ORDER BY created_at ASC",
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1, (
        f"expected 1 __improve__ row, got {len(rows)}\n"
        f"stdout:\n{rp.stdout}\nstderr:\n{rp.stderr}"
    )
    trace_id, task_class, status, created_at = rows[0]
    assert task_class == "__improve__"
    assert status == "success", f"final status {status!r}"
    assert trace_id.startswith("tr-improve-"), f"trace_id={trace_id!r}"
    assert "T" in created_at, f"created_at missing ISO T: {created_at!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (8) workflow_candidates: 3 rows, ids match the wc- regex
# ─────────────────────────────────────────────────────────────────────────────
def test_workflow_candidates(tmp_path):
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_py, TRACE_ROWS)

    rp = _py(home_py, db_py, [])
    assert rp.returncode == 0

    con = sqlite3.connect(db_py)
    try:
        rows = con.execute(
            "SELECT candidate_id, base_workflow_version_id, status, "
            "created_by FROM workflow_candidates "
            "WHERE created_by='evolution_engine' "
            "ORDER BY candidate_id ASC",
        ).fetchall()
    finally:
        con.close()
    # exactly 3 rows (limit=3 default)
    assert len(rows) == 3, f"{len(rows)} workflow_candidates"
    # group_propose parents from perf_summary randomly — base_workflow
    # version_id is whichever parent won each draw (code_fix or
    # refactor_audit). Set membership assertion only.
    for cid, base_vid, status, created_by in rows:
        assert CAND_ID_RE.match(cid), f"bad cid {cid!r}"
        assert base_vid in ("code-fix_v0.1.0", "refactor-audit_v0.1.0"), (
            f"unexpected base_vid {base_vid!r}"
        )
        assert status == "candidate", f"status {status!r}"
        assert created_by == "evolution_engine", f"created_by {created_by!r}"
