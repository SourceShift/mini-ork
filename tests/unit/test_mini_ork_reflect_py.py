"""Golden and behavioral contracts for the Python-sole reflect entrypoint.

The pre-retirement verifier captured byte-identical Bash/Python behavior before
the legacy entrypoint was removed. These tests preserve that verified contract
without retaining the retired runtime as an oracle.

8 cases (matching the kickoff's >=6 contract):
  (a) --help                          — usage text + exit 0
  (b) --dry-run empty DB              — 2 stdout lines, exit 0
  (c) --dry-run populated + filter    — 3 stdout lines (with filter line)
  (d) happy-path default (all ON)     — full pipeline + side-channels + traces
  (e) opt-out MO_PATTERN_MINER=0      — pattern_miner echo absent, [learning] still 0 patterns
  (f) unknown flag --bogus            — exit 2 + stderr "Unknown flag"
  (g) --since arg passthrough         — echo reflects passed timestamp
  (h) combined opt-out MO_RHO_AGGREGATE=0 + MO_LANE_ROUTER=0 — only [learning] line
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
INIT_SH = REPO / "db" / "init.sh"
PY_MODULE = "mini_ork.ported.mini_ork_reflect"

FIXED_SINCE = "1700000000"
EXPECTED_HELP = (
    "Usage: mini-ork reflect [--since <timestamp>] [--task-class <name>] [--dry-run]\n"
    "\n"
    "Run the reflection pipeline over recent execution traces to extract gradient\n"
    "signals, recurring patterns, and suggested workflow promotions.\n"
    "\n"
    "Options:\n"
    "  --since <timestamp>   Start of analysis window (ISO-8601 or unix ts, default: 24h ago)\n"
    "  --task-class <name>   Limit reflection to traces of this task class\n"
    "  --lane <lane>          Resolve reflection model from agents.yaml (default: reflector)\n"
    "  --dry-run             Show trace count that would be analyzed; skip LLM\n"
    "  --help                Show this help\n"
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path_factory, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh AND point the in-process
    Python port at it via `os.environ["MINI_ORK_DB"]`. The Python port reads
    this env var to resolve the DB path; the subprocess receives it via
    `_run_py`."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MO_REFLECTION_BATCH", "500")
    monkeypatch.setenv("MO_DEDUP_BATCH", "10000")
    monkeypatch.setenv("MO_DEDUP_FUZZY", "0.55")
    monkeypatch.setenv("MINI_ORK_STALE_DAYS", "14")
    monkeypatch.setenv("MINI_ORK_PROMOTION_MIN_FREQ", "3")
    monkeypatch.setenv("MO_GRADIENT_DEDUP_SIM", "0")
    return dbp


# ── Helpers ─────────────────────────────────────────────────────────────────

def _run_py(args: list[str], env_extra: dict | None = None,
            wrap_gradients: bool = False) -> subprocess.CompletedProcess:
    """Invoke the Python port via `python3 -m mini_ork.ported.mini_ork_reflect`.

    For happy-path cases, set MINI_ORK_GRADIENT_EXTRACTOR_FN=_rfl_stub in env.
    The ported mini_ork_reflect.main() reads this var and looks up `_rfl_stub`
    in `mini_ork.ported.reflection_pipeline`'s globals, installing it as the
    gradient_extract injection. This matches the bash CLI's
    `MINI_ORK_GRADIENT_EXTRACTOR_FN` semantics.

    The port reads MINI_ORK_DB / MINI_ORK_HOME / MINI_ORK_ROOT from env.
    """
    env = {**os.environ, "MO_GRADIENT_DEDUP_SIM": "0",
           "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    if env_extra:
        env.update(env_extra)
    if wrap_gradients:
        env["MINI_ORK_GRADIENT_EXTRACTOR_FN"] = "_rfl_stub"
    return subprocess.run(
        [sys.executable, "-m", PY_MODULE, *args],
        env=env, capture_output=True, text=True,
    )


def _seed_two_traces(db_path: str) -> None:
    """Insert 2 execution_traces so the dry-run branch has a non-zero count.
    Both use task_class='code_review', so that filter matches both rows."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    # Need a parent runs row + epic row (foreign keys).
    con.execute(
        "INSERT OR IGNORE INTO epics(id, title, status) VALUES ('e-rfl', 't', 'in progress')"
    )
    con.execute(
        "INSERT INTO runs(epic_id, run_dir, branch, baseline_sha, agent) "
        "VALUES ('e-rfl', 'run-rfl', 'main', 'sha', 'glm')"
    )
    run_id = con.execute("SELECT id FROM runs WHERE epic_id='e-rfl' ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.execute(
        "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
        "VALUES ('trace-A', ?, 'code_review', 'success', '2026-07-04T12:00:00.000Z')",
        (run_id,),
    )
    con.execute(
        "INSERT INTO execution_traces(trace_id, run_id, task_class, status, created_at) "
        "VALUES ('trace-B', ?, 'code_review', 'failure', '2026-07-04T12:00:01.000Z')",
        (run_id,),
    )
    con.commit()
    con.close()


def _row_counts(db_path: str) -> dict[str, int]:
    """Return a dict of {table: count} for the tables the reflect path touches."""
    tables = [
        "execution_traces", "emergent_patterns", "pattern_records",
        "bug_reports", "prompt_win_rates", "lane_router_state",
        "lane_domain_advantage", "lane_region_advantage",
        "agent_performance_memory", "gradient_records", "failure_links",
    ]
    counts = {}
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    for t in tables:
        try:
            counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[t] = -1  # table doesn't exist on this schema
    con.close()
    return counts


def _normalize_dynamic_stdout(text: str) -> str:
    """Normalize process-specific trace ids before comparing stdout."""
    return re.sub(r"tr-reflect-\d+-\d+", "tr-reflect-TS-PID", text)


# ─────────────────────────────────────────────────────────────────────────────
# (a) --help — usage text + exit 0
# ─────────────────────────────────────────────────────────────────────────────
def test_help_flag(temp_db):
    """The captured help golden is emitted on stdout with exit 0."""
    rp = _run_py(["--help"])
    assert rp.returncode == 0, f"py --help failed: {rp.stderr}"
    assert rp.stdout == EXPECTED_HELP


# ─────────────────────────────────────────────────────────────────────────────
# (b) --dry-run empty DB — 2 stdout lines, exit 0
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_empty_db(temp_db):
    """Print 2 dry-run lines on stdout, exit 0. Count is 0 on empty DB."""
    rp = _run_py(["--dry-run", "--since", FIXED_SINCE], env_extra={"MINI_ORK_DB": temp_db})
    assert rp.returncode == 0, f"py dry-run failed: {rp.stderr}"
    assert rp.stdout.startswith(f"[dry-run] would analyze 0 trace(s) since {FIXED_SINCE}\n")
    assert "[dry-run] lane: reflector ->" in rp.stdout
    assert len(rp.stdout.splitlines()) == 2
    # No execution_traces written by dry-run.
    counts = _row_counts(temp_db)
    assert counts["execution_traces"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (c) --dry-run populated + filter — 3 stdout lines (with filter line)
# ─────────────────────────────────────────────────────────────────────────────
def test_dry_run_populated_with_filter(temp_db):
    """Print 3 lines and count both seeded `code_review` rows."""
    _seed_two_traces(temp_db)
    rp = _run_py(["--dry-run", "--since", FIXED_SINCE, "--task-class", "code_review"],
                 env_extra={"MINI_ORK_DB": temp_db})
    assert rp.returncode == 0, f"py dry-run failed: {rp.stderr}"
    assert f"[dry-run] would analyze 2 trace(s) since {FIXED_SINCE}" in rp.stdout
    assert "[dry-run] lane: reflector ->" in rp.stdout
    assert "[dry-run] filter: task_class=code_review" in rp.stdout
    assert len(rp.stdout.splitlines()) == 3


# ─────────────────────────────────────────────────────────────────────────────
# (d) happy-path default (all ON) — full pipeline + side-channels + traces
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_default():
    """Run the full pipeline on a fresh DB and lock down its output and writes."""
    fresh_py_db = _init_fresh_db()
    _seed_two_traces(fresh_py_db)

    rp = _run_py(["--since", FIXED_SINCE], env_extra={"MINI_ORK_DB": fresh_py_db}, wrap_gradients=True)

    assert rp.returncode == 0, f"py happy-path failed: {rp.stderr}"

    rp_lines = _normalize_dynamic_stdout(rp.stdout).splitlines()
    assert any("=== mini-ork reflect ===" in ln for ln in rp_lines)
    assert any("[learning] persisted" in ln for ln in rp_lines)
    assert any("reflect: analyzed" in ln for ln in rp_lines)
    assert any("[pattern_miner]" in ln for ln in rp_lines)
    assert any("[cross_epic_gradient]" in ln for ln in rp_lines)
    assert any("[bug_report_sweep]" in ln for ln in rp_lines)
    assert any("[lane_router]" in ln for ln in rp_lines)

    cp = _row_counts(fresh_py_db)
    # The 2 trace-A/trace-B seed rows plus the reflect trace row gives >=3
    # execution_traces. Start/end writes share a trace_id, so trace_store upserts.
    assert cp["execution_traces"] >= 3, f"unexpected trace count: {cp['execution_traces']}"


def _init_fresh_db() -> str:
    """Init a one-off temp DB (no fixture). Returns the DB path."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        home = td
        dbp = os.path.join(td, "state.db")
        subprocess.run(
            ["bash", str(INIT_SH)],
            env={**os.environ, "MINI_ORK_HOME": home, "MINI_ORK_DB": dbp,
                 "MO_GRADIENT_DEDUP_SIM": "0"},
            capture_output=True, text=True, check=True,
        )
        # Copy to a stable path (the tempdir would be deleted at function exit).
        stable = os.path.join("/tmp", f"rfl-test-{os.getpid()}-{time.time_ns()}.db")
        subprocess.run(["cp", dbp, stable], check=True)
        return stable


# ─────────────────────────────────────────────────────────────────────────────
# (e) opt-out MO_PATTERN_MINER=0 — pattern_miner echo absent, [learning] still 0
# ─────────────────────────────────────────────────────────────────────────────
def test_opt_out_pattern_miner(temp_db):
    """MO_PATTERN_MINER=0 skips the pattern_store_mine_from_traces call.
    No `[pattern_miner]` line is emitted, while `[learning]` still reports
    zero persisted patterns."""
    _seed_two_traces(temp_db)
    extra = {"MINI_ORK_DB": temp_db, "MO_PATTERN_MINER": "0"}
    rp = _run_py(["--since", FIXED_SINCE], env_extra=extra, wrap_gradients=True)
    assert rp.returncode == 0, f"py failed: {rp.stderr}"
    assert "[pattern_miner]" not in rp.stdout, (
        f"expected no [pattern_miner] line, got: {rp.stdout!r}"
    )
    assert "[learning] persisted 0 patterns," in rp.stdout


# ─────────────────────────────────────────────────────────────────────────────
# (f) unknown flag --bogus — exit 2 + stderr "Unknown flag"
# ─────────────────────────────────────────────────────────────────────────────
def test_unknown_flag_exits_2(temp_db):
    """Unknown flags preserve the captured error message and exit code."""
    rp = _run_py(["--bogus"], env_extra={"MINI_ORK_DB": temp_db})
    assert rp.returncode == 2, f"py bogus: exit={rp.returncode} stderr={rp.stderr!r}"
    assert rp.stderr == "Unknown flag: --bogus. Try --help\n"


# ─────────────────────────────────────────────────────────────────────────────
# (g) --since arg passthrough — echo reflects passed timestamp
# ─────────────────────────────────────────────────────────────────────────────
def test_since_arg_passthrough(temp_db):
    """Echo the user-supplied SINCE in dry-run stdout."""
    fixed_since = 1700000000  # arbitrary fixed ts
    rp = _run_py(["--dry-run", "--since", str(fixed_since)],
                 env_extra={"MINI_ORK_DB": temp_db})
    assert rp.returncode == 0, f"py failed: {rp.stderr}"
    assert f"since {fixed_since}" in rp.stdout


# ─────────────────────────────────────────────────────────────────────────────
# (h) combined opt-out MO_RHO_AGGREGATE=0 + MO_LANE_ROUTER=0
# ─────────────────────────────────────────────────────────────────────────────
def test_combined_opt_out(temp_db):
    """Skip rho_aggregate + lane_router when env-disabling. Stdout should
    not contain those echo lines. The other side-channels still run."""
    _seed_two_traces(temp_db)
    extra = {
        "MINI_ORK_DB": temp_db,
        "MO_RHO_AGGREGATE": "0",
        "MO_LANE_ROUTER": "0",
    }
    rp = _run_py(["--since", FIXED_SINCE], env_extra=extra, wrap_gradients=True)
    assert rp.returncode == 0, f"py failed: {rp.stderr}"
    assert "[rho_aggregate]" not in rp.stdout
    assert "[lane_router]" not in rp.stdout
    # Other side-channels still ran.
    assert "[pattern_miner]" in rp.stdout
    assert "[cross_epic_gradient]" in rp.stdout
    assert "[bug_report_sweep]" in rp.stdout
