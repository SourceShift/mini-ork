"""Unit tests: mini_ork.cli.metrics (bash parity halves removed; formerly vs bin/mini-ork-metrics).

Covers: empty DB (default + future --since), populated DB (markdown + JSON,
with --recipe filter and --format json), and the CLI error paths (--help,
unknown flag, missing DB). Each case drives the Python port against a temp
state.db seeded via db/init.sh and asserts the collected data + rendered
outputs semantically.

JSON is checked via json.loads + 1e-6 tolerance on cost/total fields;
markdown is checked for its structural markers.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import metrics as pm


def _scenario(tmp_path: Path):
    """Init a fresh state.db via db/init.sh. Returns (home, db)."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"db/init.sh failed: {r.stderr}"
    return home, db


def _seed(db: str, task_rows, trace_rows=None, grad_rows=None):
    """Insert deterministic rows. created_at/ended_at are pinned ints (unix sec).

    trace_rows: list of (trace_id, task_class, status, duration_ms, created_at_iso).
    """
    con = sqlite3.connect(db)
    try:
        for row in task_rows:
            con.execute(
                "INSERT INTO task_runs (id, task_class, recipe, status, cost_usd, "
                "duration_ms, kickoff_path, created_at, updated_at, ended_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        if trace_rows:
            for trace_id, task_class, status, duration_ms, created_iso in trace_rows:
                con.execute(
                    "INSERT INTO execution_traces (trace_id, task_class, status, "
                    "duration_ms, created_at) VALUES (?,?,?,?,?)",
                    (trace_id, task_class, status, duration_ms, created_iso),
                )
        if grad_rows:
            for gid, target, signal, change, evidence, confidence, created in grad_rows:
                con.execute(
                    "INSERT INTO gradient_records (gradient_id, target, signal, "
                    "suggested_change, evidence, confidence, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (gid, target, signal, change, evidence, confidence, created),
                )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Test data — pinned timestamps so JSON/markdown are reproducible.
# ---------------------------------------------------------------------------
# Pinned to 2026-01-15 12:00 UTC → epoch 1768526400
PIN_BASE = 1768526400
# 24h before
PIN_DAY_BEFORE = PIN_BASE - 86400
# Now-ish (used to exclude everything via --since)
PIN_NOW = PIN_BASE + 7 * 86400  # 7 days after the seed → all seed rows excluded


def _seed_three_cycles(db: str):
    """3 task_runs (refactor-audit ×2 + code-fix ×1) + 4 traces + 2 gradients."""
    task_rows = [
        # (id, task_class, recipe, status, cost_usd, duration_ms, kickoff_path,
        #  created_at, updated_at, ended_at)
        ("run-001-aaaaaaaaaaaa", "code_fix", "refactor-audit", "published",
         0.1234, 60000, "kickoffs/r1.md",
         PIN_BASE, PIN_BASE, PIN_BASE + 60),
        ("run-002-bbbbbbbbbbbb", "code_fix", "refactor-audit", "published",
         0.0567, 90000, "kickoffs/r2.md",
         PIN_BASE + 60, PIN_BASE + 60, PIN_BASE + 150),
        ("run-003-cccccccccccc", "code_fix", "code-fix", "published",
         0.0234, 30000, "kickoffs/r3.md",
         PIN_BASE + 150, PIN_BASE + 150, PIN_BASE + 180),
    ]
    trace_rows = [
        # (trace_id, task_class, status, duration_ms, created_at_iso)
        ("trace-001", "code_fix", "success", 5000, "2026-01-15T12:00:30.000Z"),
        ("trace-002", "code_fix", "success", 8000, "2026-01-15T12:01:00.000Z"),
        ("trace-003", "code_fix", "success", 7000, "2026-01-15T12:01:30.000Z"),
        ("trace-004", "code_fix", "success", 4000, "2026-01-15T12:02:00.000Z"),
    ]
    grad_rows = [
        ("g1", "refactor-audit", "missing tests", "add coverage",
         "no test file", 0.7, PIN_BASE + 30),
        ("g2", "code-fix", "long wall", "split steps",
         "wall_secs > 120", 0.5, PIN_BASE + 200),
    ]
    _seed(db, task_rows, trace_rows, grad_rows)


# ---------------------------------------------------------------------------
# (a) empty DB → 'no cycles' markdown; JSON cycle_count=0
# ---------------------------------------------------------------------------
def test_empty_db_markdown_and_json(tmp_path):
    home, db = _scenario(tmp_path)
    # pin --since so the Window line is stable
    PIN_EPOCH = 1700000000  # 2023-11-14T22:13:20 UTC
    data = pm.collect_cycles(db, recipe_filter="", since=PIN_EPOCH)
    rp_md = pm.render_markdown(data)
    assert "_No cycles in window._" in rp_md
    rp_json = pm.render_json(data)
    parsed = json.loads(rp_json)
    assert parsed["totals"]["cycle_count"] == 0
    assert parsed["cycles"] == []


# ---------------------------------------------------------------------------
# (b) populated DB → JSON totals + markdown markers
# ---------------------------------------------------------------------------
def test_populated_db(tmp_path):
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    # pin --since to PIN_DAY_BEFORE so all 3 cycles fall in window
    rp_data = pm.collect_cycles(db, recipe_filter="", since=PIN_DAY_BEFORE)
    rp_md = pm.render_markdown(rp_data)
    # markdown mentions the runs + totals
    assert "run-001-aaaaaaaaaaaa" in rp_md
    assert "run-003-cccccccccccc" in rp_md

    rp_json = pm.render_json(rp_data)
    parsed_p = json.loads(rp_json)
    assert parsed_p["totals"]["cycle_count"] == 3
    assert abs(parsed_p["totals"]["total_cost_usd"] - (0.1234 + 0.0567 + 0.0234)) < 1e-6
    assert parsed_p["totals"]["trace_count"] == 4
    assert parsed_p["totals"]["gradient_count"] == 2
    assert len(parsed_p["cycles"]) == 3
    costs = [c["cost_usd"] for c in parsed_p["cycles"]]
    assert abs(costs[0] - 0.1234) < 1e-6
    assert abs(costs[1] - 0.0567) < 1e-6
    assert abs(costs[2] - 0.0234) < 1e-6


# ---------------------------------------------------------------------------
# (c) --recipe filter narrows to 1 row
# ---------------------------------------------------------------------------
def test_recipe_filter(tmp_path):
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    rp_data = pm.collect_cycles(db, recipe_filter="code-fix", since=PIN_DAY_BEFORE)
    rp_md = pm.render_markdown(rp_data)
    assert "run-003-cccccccccccc" in rp_md
    assert "run-001-aaaaaaaaaaaa" not in rp_md
    parsed = json.loads(pm.render_json(rp_data))
    assert parsed["totals"]["cycle_count"] == 1
    assert parsed["cycles"][0]["recipe"] == "code-fix"
    assert parsed["recipe_filter"] == "code-fix"


# ---------------------------------------------------------------------------
# (d) --since in the future → 'No cycles in window.'
# ---------------------------------------------------------------------------
def test_since_future_excludes_all(tmp_path):
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    rp_data = pm.collect_cycles(db, recipe_filter="", since=PIN_NOW)
    rp_md = pm.render_markdown(rp_data)
    assert "_No cycles in window._" in rp_md
    parsed = json.loads(pm.render_json(rp_data))
    assert parsed["totals"]["cycle_count"] == 0


# ---------------------------------------------------------------------------
# (e) --help → rc=0, stdout contains 'Usage:'
# ---------------------------------------------------------------------------
def test_help(tmp_path):
    home, db = _scenario(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = pm.main(["--help"], stdout=out, stderr=err)
    assert rc == 0
    assert "Usage:" in out.getvalue()


# ---------------------------------------------------------------------------
# (f) unknown flag --bogus → rc=2, stderr contains 'Unknown flag'
# ---------------------------------------------------------------------------
def test_unknown_flag(tmp_path):
    home, db = _scenario(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = pm.main(["--bogus"], stdout=out, stderr=err)
    assert rc == 2
    assert "Unknown flag" in err.getvalue()


# ---------------------------------------------------------------------------
# (g) missing DB → rc=1, stderr contains 'no state.db at'
# ---------------------------------------------------------------------------
def test_missing_db(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    nonexistent = str(home / "no-such.db")
    # port: feed MINI_ORK_DB via env, point main() at the nonexistent path
    old = os.environ.get("MINI_ORK_DB")
    os.environ["MINI_ORK_DB"] = nonexistent
    try:
        out2, err2 = io.StringIO(), io.StringIO()
        rc2 = pm.main([], stdout=out2, stderr=err2)
        assert rc2 == 1
        assert "no state.db at" in err2.getvalue()
    finally:
        if old is None:
            os.environ.pop("MINI_ORK_DB", None)
        else:
            os.environ["MINI_ORK_DB"] = old


# ---------------------------------------------------------------------------
# (h) --format json with 3 cycles
# ---------------------------------------------------------------------------
def test_format_json(tmp_path):
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    rp_data = pm.collect_cycles(db, recipe_filter="", since=PIN_DAY_BEFORE)
    rp_json = pm.render_json(rp_data)
    parsed = json.loads(rp_json)
    assert parsed["totals"]["cycle_count"] == 3


# ---------------------------------------------------------------------------
# (i) gradient_records missing-table → OperationalError branch (try/except)
# ---------------------------------------------------------------------------
def test_missing_gradient_table(tmp_path):
    """'no such table: gradient_records' is handled via try/except → 0.

    DROP the table to simulate a pre-0038 DB; gradient_count must be 0.
    """
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE gradient_records")
        con.commit()
    finally:
        con.close()

    rp_data = pm.collect_cycles(db, recipe_filter="", since=PIN_DAY_BEFORE)
    rp_json = pm.render_json(rp_data)
    parsed = json.loads(rp_json)
    assert parsed["totals"]["gradient_count"] == 0
    assert parsed["totals"]["trace_count"] == 4
