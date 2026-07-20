"""Parity gate: ``mini_ork.cli.improve`` vs ``bin/mini-ork-improve``.

Faithful Python port of bin/mini-ork-improve, locked by a parity test that
drives LIVE bash subprocess vs ``python3 -m mini_ork.cli.improve``
against a ``db/init.sh``-seeded tmp DB. No mocks, no hardcoded outputs:
every expected output is whatever the live bash produced in this run.

Cases (>=6):
  (1) --help long     — rc=0, usage on stdout, stderr empty.
  (2) -h short        — same as (1).
  (3) unknown flag    — rc=2, stderr contains 'Unknown flag' + the bad arg.
  (4) --dry-run       — seeded DB; stdout matches bash modulo perf_summary
                          field order (json.loads round-trip + assertEqual
                          on parsed dicts).
  (5) --task-class    — dry-run with scope line; perf_summary narrowed to
                          ONE task_class only (WHERE clause parity).
  (6) happy path      — seeded DB; '=== mini-ork improve ===', 'Proposed
                          candidates:', 3x 'candidate_id=wc-', trailing raw
                          IDs; framing text byte-equal modulo candidate IDs.
  (7) DB-row diff     — bash and py each UPSERT 1 execution_traces row
                          with task_class='__improve__' (running → success
                          same trace_id); assert count=1 + status='success'
                          on each runtime's DB.
  (8) workflow_candidates rows   — bash and py each write N=limit=3 rows
                          to workflow_candidates; ids match /wc-[0-9a-f]+/.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

BASH = REPO / "bin" / "mini-ork-improve"
PY_MOD = "mini_ork.cli.improve"

CAND_ID_RE = re.compile(r"^wc-[0-9a-f]{6,}$")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "python3", "sqlite3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not BASH.exists():
        pytest.skip(f"missing bin/mini-ork-improve at {BASH}")


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


def _bash(home: Path, db: str, args, *, extra_env=None):
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": db,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(BASH)] + list(args),
        capture_output=True, text=True, env=env, cwd=str(REPO),
    )


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


def _strip_dynamic_ids(s: str) -> str:
    """Drop candidate_id=N lines AND standalone wc-XXX id lines.

    Used to byte-compare happy-path stdout modulo candidate-id content
    (which is random per-invocation; bash and py pick different ids).
    Both runtimes pad ``  candidate_id=...`` with 2 leading spaces; we
    lstrip before testing so the indented per-candidate lines get
    matched by the wc- regex too.
    """
    out_lines = []
    for ln in s.splitlines():
        trimmed = ln.lstrip()
        if trimmed.startswith("candidate_id="):
            continue
        if CAND_ID_RE.match(trimmed):
            continue
        out_lines.append(ln)
    return "\n".join(out_lines)


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
    _which_tools()
    home_bash = tmp_path / "bash"; home_bash.mkdir()
    home_py = tmp_path / "py"; home_py.mkdir()
    rb = _bash(home_bash, str(home_bash / "state.db"), ["--help"])
    rp = _py(home_py, str(home_py / "state.db"), ["--help"])
    assert rb.returncode == 0 == rp.returncode
    assert rb.stdout == rp.stdout, (
        f"stdout differs\n--- bash ---\n{rb.stdout}\n--- py ---\n{rp.stdout}"
    )
    assert "Usage: mini-ork improve" in rp.stdout
    assert "--task-class" in rp.stdout
    assert "--limit" in rp.stdout
    assert "--dry-run" in rp.stdout
    assert rb.stderr == rp.stderr == ""


# ─────────────────────────────────────────────────────────────────────────────
# (2) -h short
# ─────────────────────────────────────────────────────────────────────────────
def test_help_short_flag(tmp_path):
    _which_tools()
    home_bash = tmp_path / "bash"; home_bash.mkdir()
    home_py = tmp_path / "py"; home_py.mkdir()
    rb = _bash(home_bash, str(home_bash / "state.db"), ["-h"])
    rp = _py(home_py, str(home_py / "state.db"), ["-h"])
    assert rb.returncode == 0 == rp.returncode
    assert rb.stdout == rp.stdout
    assert "Usage: mini-ork improve" in rp.stdout
    assert rb.stderr == rp.stderr == ""


# ─────────────────────────────────────────────────────────────────────────────
# (3) unknown flag -> rc=2 + stderr 'Unknown flag'
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_flag_exits_2(tmp_path):
    _which_tools()
    home_bash = tmp_path / "bash"; home_bash.mkdir()
    home_py = tmp_path / "py"; home_py.mkdir()
    rb = _bash(home_bash, str(home_bash / "state.db"), ["--nope"])
    rp = _py(home_py, str(home_py / "state.db"), ["--nope"])
    assert rb.returncode == 2 == rp.returncode
    assert "Unknown flag" in rb.stderr
    assert "Unknown flag" in rp.stderr
    # Both runtimes must echo back the offending flag.
    assert "--nope" in rb.stderr
    assert "--nope" in rp.stderr


# ─────────────────────────────────────────────────────────────────────────────
# (4) --dry-run against a seeded DB
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_against_seeded_db(tmp_path):
    _which_tools()
    home_bash, db_bash = _scenario(tmp_path / "bash")
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_bash, TRACE_ROWS)
    _seed_traces(db_py, TRACE_ROWS)

    rb = _bash(home_bash, db_bash, ["--dry-run"])
    rp = _py(home_py, db_py, ["--dry-run"])

    assert rb.returncode == 0 == rp.returncode
    assert "[dry-run] performance summary:" in rb.stdout
    assert "[dry-run] performance summary:" in rp.stdout
    assert "[dry-run] would call group_evolver with limit=3" in rb.stdout
    assert "[dry-run] would call group_evolver with limit=3" in rp.stdout
    assert rb.stderr == rp.stderr == ""

    # Framing text (header + blank line + footer line) is byte-equal; the
    # perf JSON block is structurally equal (parsed + json-roundtripped).
    bash_lines = rb.stdout.splitlines()
    py_lines = rp.stdout.splitlines()
    bash_perf = _perf_block(rb.stdout)
    py_perf = _perf_block(rp.stdout)
    # Drop the perf block + the perf header from each; what remains must be
    # byte-identical between bash and py.
    rest_b = [ln for ln in bash_lines
              if ln not in bash_perf
              and ln != "[dry-run] performance summary:"]
    rest_p = [ln for ln in py_lines
              if ln not in py_perf
              and ln != "[dry-run] performance summary:"]
    assert rest_b == rest_p, (
        f"dry-run framing mismatch:\nbash_rest={rest_b}\npy_rest={rest_p}"
    )
    # Structural diff on perf_summary (parseable JSON).
    bash_json_text = "\n".join(bash_perf)
    py_json_text = "\n".join(py_perf)
    bash_parsed = json.loads(bash_json_text) if bash_json_text.strip() else []
    py_parsed = json.loads(py_json_text) if py_json_text.strip() else []
    assert bash_parsed == py_parsed, (
        f"perf_summary content mismatch:\nbash={bash_parsed}\npy={py_parsed}"
    )
    # Both are lists of dicts with the GROUP-BY columns in declaration
    # order (task_class, total_runs, successes, avg_duration_ms, avg_cost_usd).
    for p in bash_parsed:
        assert set(p.keys()) == {
            "task_class", "total_runs", "successes",
            "avg_duration_ms", "avg_cost_usd",
        }
    # Float AVG columns within 1e-6 (parity at 1e-6 contract).
    for cb, cp in zip(bash_parsed, py_parsed):
        assert abs(cb["avg_duration_ms"] - cp["avg_duration_ms"]) < 1e-6
        assert abs(cb["avg_cost_usd"] - cp["avg_cost_usd"]) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (5) --dry-run + --task-class narrows perf_summary to one class
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_with_task_class_filter(tmp_path):
    _which_tools()
    home_bash, db_bash = _scenario(tmp_path / "bash")
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_bash, TRACE_ROWS)
    _seed_traces(db_py, TRACE_ROWS)

    rb = _bash(home_bash, db_bash,
               ["--dry-run", "--task-class", "code_fix"])
    rp = _py(home_py, db_py,
             ["--dry-run", "--task-class", "code_fix"])

    assert rb.returncode == 0 == rp.returncode
    assert "[dry-run] scope: task_class=code_fix" in rb.stdout
    assert "[dry-run] scope: task_class=code_fix" in rp.stdout

    # structural perf comparison (parity of WHERE clause)
    bash_perf = _perf_block(rb.stdout)
    py_perf = _perf_block(rp.stdout)
    bash_parsed = json.loads("\n".join(bash_perf)) if bash_perf else []
    py_parsed = json.loads("\n".join(py_perf)) if py_perf else []
    assert bash_parsed == py_parsed
    # AND the WHERE clause actually narrows to one task_class.
    assert all(p["task_class"] == "code_fix" for p in bash_parsed)
    assert all(p["task_class"] == "code_fix" for p in py_parsed)
    assert len(bash_parsed) == 1
    # And the counts reflect code_fix's 2 seed rows.
    assert bash_parsed[0]["total_runs"] == 2
    assert bash_parsed[0]["successes"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# (6) happy path: full dispatch
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_proposes_and_stores_candidates(tmp_path):
    _which_tools()
    home_bash, db_bash = _scenario(tmp_path / "bash")
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_bash, TRACE_ROWS)
    _seed_traces(db_py, TRACE_ROWS)

    rb = _bash(home_bash, db_bash, [])
    rp = _py(home_py, db_py, [])

    assert rb.returncode == 0 == rp.returncode
    for s in (rb.stdout, rp.stdout):
        assert "=== mini-ork improve ===" in s
        assert "Proposed candidates:" in s
        assert "Persisted 3 candidate(s) to workflow_candidates table." in s
        assert s.count("candidate_id=wc-") == 3

    # Framing text byte-equal modulo candidate-id lines + trailing ids.
    assert _strip_dynamic_ids(rb.stdout) == _strip_dynamic_ids(rp.stdout)
    # Last N lines (== candidate_limit == 3) of each stdout are raw IDs.
    bash_lines = rb.stdout.splitlines()
    py_lines = rp.stdout.splitlines()
    for line in bash_lines[-3:] + py_lines[-3:]:
        assert CAND_ID_RE.match(line), f"bad id line: {line!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (7) execution_traces row diff after non-dry-run
# ─────────────────────────────────────────────────────────────────────────────
def test_improve_exec_traces_row_diff(tmp_path):
    _which_tools()
    home_bash, db_bash = _scenario(tmp_path / "bash")
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_bash, TRACE_ROWS)
    _seed_traces(db_py, TRACE_ROWS)

    rb = _bash(home_bash, db_bash, [])
    rp = _py(home_py, db_py, [])
    assert rb.returncode == 0 == rp.returncode

    # Each runtime UPSERTs (running → success same trace_id) so the final
    # state is exactly 1 row per runtime with status='success'.
    for db, runtime, r in ((db_bash, "bash", rb),
                            (db_py,   "py",   rp)):
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT trace_id, task_class, status, created_at "
                "FROM execution_traces WHERE task_class='__improve__' "
                "ORDER BY created_at ASC",
            ).fetchall()
        finally:
            con.close()
        assert len(rows) == 1, (
            f"{runtime} expected 1 __improve__ row, got {len(rows)}\n"
            f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        )
        trace_id, task_class, status, created_at = rows[0]
        assert task_class == "__improve__", f"{runtime}: bad task_class"
        assert status == "success", f"{runtime}: final status {status!r}"
        assert trace_id.startswith("tr-improve-"), (
            f"{runtime}: trace_id={trace_id!r}"
        )
        assert "T" in created_at, (
            f"{runtime}: created_at missing ISO T: {created_at!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (8) workflow_candidates parity: 3 rows per runtime, ids match the wc- regex
# ─────────────────────────────────────────────────────────────────────────────
def test_workflow_candidates_parity(tmp_path):
    _which_tools()
    home_bash, db_bash = _scenario(tmp_path / "bash")
    home_py, db_py = _scenario(tmp_path / "py")
    _seed_traces(db_bash, TRACE_ROWS)
    _seed_traces(db_py, TRACE_ROWS)

    rb = _bash(home_bash, db_bash, [])
    rp = _py(home_py, db_py, [])
    assert rb.returncode == 0 == rp.returncode

    for db, runtime in ((db_bash, "bash"), (db_py, "py")):
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "SELECT candidate_id, base_workflow_version_id, status, "
                "created_by FROM workflow_candidates "
                "WHERE created_by='evolution_engine' "
                "ORDER BY candidate_id ASC",
            ).fetchall()
        finally:
            con.close()
        # Bash and py each write exactly 3 rows (limit=3 default).
        assert len(rows) == 3, f"{runtime}: {len(rows)} workflow_candidates"
        # group_propose parents from perf_summary randomly — base_workflow
        # version_id is whichever parent won each draw (code_fix or
        # refactor_audit). Set membership assertion only.
        for cid, base_vid, status, created_by in rows:
            assert CAND_ID_RE.match(cid), f"{runtime}: bad cid {cid!r}"
            assert base_vid in ("code-fix_v0.1.0", "refactor-audit_v0.1.0"), (
                f"{runtime}: unexpected base_vid {base_vid!r}"
            )
            assert status == "candidate", f"{runtime}: status {status!r}"
            assert created_by == "evolution_engine", (
                f"{runtime}: created_by {created_by!r}"
            )
