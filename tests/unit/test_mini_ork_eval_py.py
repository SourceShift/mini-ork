"""Parity gate: ``mini_ork.ported.mini_ork_eval`` vs ``bin/mini-ork-eval``.

Each test drives the LIVE bash entry point via subprocess against the
SAME SQLite fixture as the Python port, then deep-compares both
outputs. No mocks, no hardcoded outputs beyond what bash itself emits.

Schema bootstrap: every fixture is initialised by ``db/init.sh`` which
applies all migrations 0001..0047 and gives both bash and Python the
canonical schema (``workflow_candidates``, ``workflow_memory.yaml_blob``,
``benchmark_tasks``, ``benchmark_results``). We seed candidate rows with
a synthetic workflow_memory entry so the JOIN
``workflow_candidates→workflow_memory.yaml_blob`` resolves.

Eight cases (above the kickoff's >=6 floor):
  (a) --help             — both exit 0, stdout bytes equal
  (b) no --candidate     — both exit 2, "Usage:" emitted
  (c) unknown candidate  — both exit 2, 4-line "Candidate not found" stderr
  (d) --dry-run          — both emit benchmark_list JSON + "[dry-run] ..." line
                            (JSON parsed via json.loads for structural compare)
  (e) happy-path non-dry — both exit 0; stdout contains all expected substrings
                            AND workflow_candidates row matches bash byte-for-byte
                            on stable columns (status, utility_delta,
                            base_workflow_version_id)
  (f) --suite code_fix dry — both exit 0; benchmark_list filtered by task_class
  (g) stdout header/footer — both emit the literal "=== mini-ork eval ===" and
                              "=== eval result ===" blocks
  (h) DB OperationalError — chmod 0444 the DB file, python port must print
                              "[warn] DB update skipped" to stderr AND exit 0

Float tolerance: 1e-6 for any float fields (utility_delta etc.). Skip
gracefully on missing bash/python3.
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

BASH_BIN = REPO / "bin" / "mini-ork-eval"
PY_MOD = "mini_ork.ported.mini_ork_eval"

_FLOAT_TOL = 1e-6
CANDIDATE_ID = "cand-001"
BASE_VERSION_ID = "wf-v1"
YAML_BLOB = "name: test-workflow\nsteps: []\n"


def _which_tools() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH")
    if not BASH_BIN.exists():
        pytest.skip(f"missing bin/mini-ork-eval at {BASH_BIN}")


def _seed_candidate(db: str, candidate_id: str = CANDIDATE_ID) -> None:
    """Seed workflow_memory + workflow_candidates so the JOIN resolves."""
    con = sqlite3.connect(db)
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO workflow_memory
              (workflow_version_id, workflow_name, yaml_hash, yaml_blob,
               mutations, status, created_at)
            VALUES (?, 'test-workflow', 'h1', ?, '[]', 'candidate',
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (BASE_VERSION_ID, YAML_BLOB),
        )
        con.execute(
            """
            INSERT OR REPLACE INTO workflow_candidates
              (candidate_id, base_workflow_version_id, mutations, status,
               utility_delta, created_by, created_at)
            VALUES (?, ?, '[]', 'candidate', 0.0, 'evolution_engine',
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (candidate_id, BASE_VERSION_ID),
        )
        con.commit()
    finally:
        con.close()


def _seed_task(db: str, task_class: str, benchmark_id: str) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO benchmark_tasks
              (benchmark_id, task_class, input_payload, expected_artifact_hash,
               expected_criteria, success_verifiers, baseline_utility_score,
               source, created_at)
            VALUES (?, ?, '{}', '', '{}', '[]', 0.5, 'human',
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (benchmark_id, task_class),
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def db(tmp_path_factory):
    """Fresh DB initialised via db/init.sh (canonical 0047 schema)."""
    _which_tools()
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


@pytest.fixture
def seeded_db(db):
    """db + one workflow_memory row + one workflow_candidates row."""
    _seed_candidate(db)
    return db


def _run_bash(args: list[str], db: str, env_extra: dict | None = None):
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}
    if env_extra:
        env.update(env_extra)
    cmd = ["bash", str(BASH_BIN)] + list(args)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       cwd=str(REPO))
    return r.returncode, r.stdout, r.stderr


def _run_py(args: list[str], db: str, env_extra: dict | None = None):
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}
    if env_extra:
        env.update(env_extra)
    cmd = ["python3", "-m", PY_MOD] + list(args)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       cwd=str(REPO))
    return r.returncode, r.stdout, r.stderr


def _row(db: str, candidate_id: str) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT status, utility_delta, base_workflow_version_id "
            "FROM workflow_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
    finally:
        con.close()
    return dict(row) if row else {}


# ─────────────────────────────────────────────────────────────────────────────
# (a) --help: both exit 0, stdout bytes equal
# ─────────────────────────────────────────────────────────────────────────────
def test_help_parity(db):
    bash_rc, bash_out, _ = _run_bash(["--help"], db)
    py_rc, py_out, _ = _run_py(["--help"], db)
    assert bash_rc == 0 == py_rc
    assert bash_out == py_out
    assert "Usage: mini-ork eval" in py_out


# ─────────────────────────────────────────────────────────────────────────────
# (b) no --candidate: both exit 2, "Usage:" emitted (bash writes to stdout)
# ─────────────────────────────────────────────────────────────────────────────
def test_no_candidate_parity(db):
    bash_rc, bash_out, bash_err = _run_bash([], db)
    py_rc, py_out, py_err = _run_py([], db)
    assert bash_rc == 2 == py_rc
    # bash emits _usage on stdout (cat <<EOF); python mirrors that.
    assert "Usage: mini-ork eval" in bash_out
    assert "Usage: mini-ork eval" in py_out
    assert bash_out == py_out
    assert bash_err == py_err == ""


# ─────────────────────────────────────────────────────────────────────────────
# (c) unknown candidate: both exit 2 with 4-line "Candidate not found" stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_candidate_parity(db):
    bash_rc, bash_out, bash_err = _run_bash(["--candidate", "ghost-xyz"], db)
    py_rc, py_out, py_err = _run_py(["--candidate", "ghost-xyz"], db)
    assert bash_rc == 2 == py_rc
    assert bash_out == py_out == ""
    assert "Candidate not found in DB: ghost-xyz" in bash_err
    assert "Candidate not found in DB: ghost-xyz" in py_err
    # 4-line stderr block identical between bash and python
    assert bash_err == py_err
    # Block content parity
    assert "FK gap" in bash_err
    assert "mini-ork improve" in bash_err


# ─────────────────────────────────────────────────────────────────────────────
# (d) --dry-run: benchmark_list JSON + "[dry-run] ..." line
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_parity(seeded_db):
    _seed_task(seeded_db, "default", "bt-d-1")
    bash_rc, bash_out, bash_err = _run_bash(
        ["--candidate", CANDIDATE_ID, "--dry-run"], seeded_db
    )
    py_rc, py_out, py_err = _run_py(
        ["--candidate", CANDIDATE_ID, "--dry-run"], seeded_db
    )
    assert bash_rc == 0 == py_rc
    assert bash_err == py_err == ""
    assert bash_out == py_out

    # Pull the JSON line from the output (between header and [dry-run] line)
    lines = bash_out.splitlines()
    json_line = next(
        ln for ln in lines if ln.startswith("[") and ln.endswith("]")
    )
    parsed = json.loads(json_line)
    assert isinstance(parsed, list)
    assert len(parsed) >= 1
    assert parsed[0]["benchmark_id"] == "bt-d-1"
    assert parsed[0]["task_class"] == "default"

    # The literal "would run each task" line must follow
    assert any(
        "[dry-run] would run each task with candidate workflow=" + CANDIDATE_ID in ln
        for ln in lines
    )


# ─────────────────────────────────────────────────────────────────────────────
# (e) happy-path non-dry-run: exit 0; substrings present + DB row diff
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_parity(seeded_db):
    bash_rc, bash_out, bash_err = _run_bash(
        ["--candidate", CANDIDATE_ID], seeded_db
    )
    py_rc, py_out, py_err = _run_py(
        ["--candidate", CANDIDATE_ID], seeded_db
    )
    assert bash_rc == 0 == py_rc
    assert bash_err == py_err == ""

    # Both emit the same structural blocks (substrings, not byte-equality —
    # bash's benchmark_run summary JSON carries candidate_id='--suite' due
    # to a latent bash bug; python fixes it to the real candidate_id).
    for out in (bash_out, py_out):
        assert "=== mini-ork eval ===" in out
        assert "=== eval result ===" in out
        assert f"    candidate: {CANDIDATE_ID}" in out
        assert "    tasks_evaluated: 0" in out
        assert "    total_utility:   0" in out
        assert "    baseline_utility:0" in out
        assert "    utility_delta:   0" in out
        assert "utility_delta=0" in out  # bash uses print(0) → "0"

    # DB row diff on stable columns (per kickoff: status, utility_delta,
    # base_workflow_version_id). Both sides must produce the same shape.
    bash_row = _row(seeded_db, CANDIDATE_ID)
    assert bash_row["status"] == "shadow"
    assert abs(float(bash_row["utility_delta"]) - 0.0) <= _FLOAT_TOL
    assert bash_row["base_workflow_version_id"] == BASE_VERSION_ID


def test_happy_path_db_row_parity_isolated(tmp_path_factory):
    """Re-run bash + python against FRESH separate DBs so the workflow_candidates
    row diff has zero cross-contamination. Both should produce
    status='shadow' + utility_delta=0.0 + base_workflow_version_id unchanged."""
    _which_tools()
    home_b = tmp_path_factory.mktemp("bash_home")
    home_p = tmp_path_factory.mktemp("py_home")
    db_b = str(home_b / "state.db")
    db_p = str(home_p / "state.db")
    for env_home, dbp in ((home_b, db_b), (home_p, db_p)):
        subprocess.run(
            ["bash", str(REPO / "db" / "init.sh")],
            env={**os.environ, "MINI_ORK_HOME": str(env_home),
                 "MINI_ORK_DB": dbp},
            capture_output=True, text=True, check=True,
        )
        _seed_candidate(dbp)

    bash_rc, _, _ = _run_bash(["--candidate", CANDIDATE_ID], db_b)
    py_rc, _, _ = _run_py(["--candidate", CANDIDATE_ID], db_p)
    assert bash_rc == 0 == py_rc

    bash_row = _row(db_b, CANDIDATE_ID)
    py_row = _row(db_p, CANDIDATE_ID)
    assert bash_row == py_row
    assert bash_row["status"] == "shadow"
    assert abs(float(bash_row["utility_delta"])) <= _FLOAT_TOL
    assert bash_row["base_workflow_version_id"] == BASE_VERSION_ID


# ─────────────────────────────────────────────────────────────────────────────
# (f) --suite code_fix dry-run: filtered benchmark_list output
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_filtered_suite_parity(seeded_db):
    _seed_task(seeded_db, "code_fix", "bt-cf-1")
    bash_rc, bash_out, _ = _run_bash(
        ["--candidate", CANDIDATE_ID, "--suite", "code_fix", "--dry-run"],
        seeded_db,
    )
    py_rc, py_out, _ = _run_py(
        ["--candidate", CANDIDATE_ID, "--suite", "code_fix", "--dry-run"],
        seeded_db,
    )
    assert bash_rc == 0 == py_rc
    assert bash_out == py_out

    # Filtered to just the code_fix task
    lines = bash_out.splitlines()
    json_line = next(
        ln for ln in lines if ln.startswith("[") and ln.endswith("]")
    )
    parsed = json.loads(json_line)
    assert len(parsed) == 1
    assert parsed[0]["benchmark_id"] == "bt-cf-1"
    assert parsed[0]["task_class"] == "code_fix"


# ─────────────────────────────────────────────────────────────────────────────
# (g) stdout header/footer block — exact string matching
# ─────────────────────────────────────────────────────────────────────────────
def test_stdout_header_footer_parity(seeded_db):
    bash_rc, bash_out, _ = _run_bash(["--candidate", CANDIDATE_ID], seeded_db)
    py_rc, py_out, _ = _run_py(["--candidate", CANDIDATE_ID], seeded_db)
    assert bash_rc == 0 == py_rc

    # Both must contain the literal header + footer blocks
    for out in (bash_out, py_out):
        assert "=== mini-ork eval ===" in out
        assert "=== eval result ===" in out
        assert "    candidate:       " + CANDIDATE_ID in out  # 7-space pad
        assert "    tasks_evaluated: 0" in out
        # Verify trailing utility_delta line is exact
        assert re.search(r"^utility_delta=0$", out, re.MULTILINE), (
            "trailing 'utility_delta=0' line missing or malformed"
        )


# ─────────────────────────────────────────────────────────────────────────────
# (h) DB OperationalError path: chmod 0444 the DB; port must NOT crash.
#     Skipped on macOS where sqlite3 WAL may still write.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="chmod 0444 unreliable on macOS sqlite3 WAL — see risk_note",
)
def test_db_operational_error_path(tmp_path_factory):
    _which_tools()
    home = tmp_path_factory.mktemp("readonly_home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    _seed_candidate(dbp)

    # Make the DB read-only to force an OperationalError on UPDATE
    os.chmod(dbp, 0o444)

    try:
        py_rc, py_out, py_err = _run_py(["--candidate", CANDIDATE_ID], dbp)
        assert py_rc == 0, (
            f"python port crashed on read-only DB (rc={py_rc}): {py_err}"
        )
        assert "[warn] DB update skipped" in py_err, (
            f"expected '[warn] DB update skipped' in stderr, got: {py_err!r}"
        )
        # stdout still emits the structural blocks (no abort before UPDATE)
        assert "=== mini-ork eval ===" in py_out
        assert "=== eval result ===" in py_out
    finally:
        # Restore permissions so tmp_path teardown can clean up
        os.chmod(dbp, 0o644)
        # Remove WAL sidecar if present
        for sidecar in (dbp + "-wal", dbp + "-shm"):
            try:
                os.remove(sidecar)
            except OSError:
                pass