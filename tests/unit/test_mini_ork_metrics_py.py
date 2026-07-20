"""Parity gate: mini_ork.cli.metrics vs bin/mini-ork-metrics.

Bash stays untouched (strangler-fig); the port must produce byte-identical output
to the live bash on >=6 cases covering: empty DB (default + future --since),
populated DB (markdown + JSON, with --recipe filter and --format json), and
the CLI error paths (--help, unknown flag, missing DB). Each case drives the
LIVE bash function via subprocess against the Python port on identical temp
state.db seeded via db/init.sh.

JSON is compared via json.loads + assertAlmostEqual on cost/total fields at
1e-6; markdown is compared as raw stdout strings to lock trailing newlines and
float f-string formats.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import metrics as pm

SH = REPO / "bin" / "mini-ork-metrics"


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


def _bash_metrics(home: Path, db: str, args, *, extra_env=None):
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": db,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SH), *args],
        capture_output=True, text=True, env=env,
    )


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
# 12h after
PIN_12H_AFTER = PIN_BASE + 43200
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
def test_empty_db_markdown_and_json_parity(tmp_path):
    home, db = _scenario(tmp_path)
    # pin --since for both sides so the Window line is byte-stable
    PIN_EPOCH = 1700000000  # 2023-11-14T22:13:20 UTC
    # bash markdown
    rb = _bash_metrics(home, db, ["--since", str(PIN_EPOCH)])
    data = pm.collect_cycles(db, recipe_filter="", since=PIN_EPOCH)
    rp_md = pm.render_markdown(data)
    assert rb.returncode == 0, rb.stderr
    assert rb.stdout == rp_md
    # bash JSON
    rj = _bash_metrics(home, db, ["--since", str(PIN_EPOCH), "--format", "json"])
    rp_json = pm.render_json(data)
    assert rj.returncode == 0
    assert rj.stdout == rp_json
    parsed = json.loads(rj.stdout)
    assert parsed["totals"]["cycle_count"] == 0
    assert parsed["cycles"] == []


# ---------------------------------------------------------------------------
# (b) populated DB → byte-equal JSON + markdown
# ---------------------------------------------------------------------------
def test_populated_db_parity(tmp_path):
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    # pin --since to PIN_DAY_BEFORE so all 3 cycles fall in window
    rb = _bash_metrics(home, db, ["--since", str(PIN_DAY_BEFORE)])
    rp_data = pm.collect_cycles(db, recipe_filter="", since=PIN_DAY_BEFORE)
    rp_md = pm.render_markdown(rp_data)
    assert rb.returncode == 0, rb.stderr
    # raw byte compare — lock trailing newlines + float f-string formats
    assert rb.stdout == rp_md

    rj = _bash_metrics(home, db, ["--since", str(PIN_DAY_BEFORE), "--format", "json"])
    rp_json = pm.render_json(rp_data)
    assert rj.stdout == rp_json
    parsed_b = json.loads(rj.stdout)
    parsed_p = json.loads(rp_json)
    assert parsed_b["totals"]["cycle_count"] == 3
    assert parsed_p["totals"]["cycle_count"] == 3
    # float fields within 1e-6
    assert abs(parsed_b["totals"]["total_cost_usd"] - parsed_p["totals"]["total_cost_usd"]) < 1e-6
    assert parsed_b["totals"]["trace_count"] == parsed_p["totals"]["trace_count"] == 4
    assert parsed_b["totals"]["gradient_count"] == parsed_p["totals"]["gradient_count"] == 2
    assert len(parsed_b["cycles"]) == len(parsed_p["cycles"]) == 3
    for cb, cp in zip(parsed_b["cycles"], parsed_p["cycles"]):
        assert abs(cb["cost_usd"] - cp["cost_usd"]) < 1e-6


# ---------------------------------------------------------------------------
# (c) --recipe filter narrows to 1 row
# ---------------------------------------------------------------------------
def test_recipe_filter_parity(tmp_path):
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    args = ["--since", str(PIN_DAY_BEFORE), "--recipe", "code-fix"]
    rb = _bash_metrics(home, db, args)
    rp_data = pm.collect_cycles(db, recipe_filter="code-fix", since=PIN_DAY_BEFORE)
    rp_md = pm.render_markdown(rp_data)
    assert rb.returncode == 0, rb.stderr
    assert rb.stdout == rp_md
    # JSON
    rj = _bash_metrics(home, db, [*args, "--format", "json"])
    assert rj.stdout == pm.render_json(rp_data)
    parsed = json.loads(rj.stdout)
    assert parsed["totals"]["cycle_count"] == 1
    assert parsed["cycles"][0]["recipe"] == "code-fix"
    assert parsed["recipe_filter"] == "code-fix"


# ---------------------------------------------------------------------------
# (d) --since in the future → 'No cycles in window.' on both sides
# ---------------------------------------------------------------------------
def test_since_future_excludes_all(tmp_path):
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    args = ["--since", str(PIN_NOW)]
    rb = _bash_metrics(home, db, args)
    rp_data = pm.collect_cycles(db, recipe_filter="", since=PIN_NOW)
    rp_md = pm.render_markdown(rp_data)
    assert rb.returncode == 0
    assert rb.stdout == rp_md
    assert "_No cycles in window._" in rb.stdout
    parsed = json.loads(_bash_metrics(home, db, [*args, "--format", "json"]).stdout)
    assert parsed["totals"]["cycle_count"] == 0


# ---------------------------------------------------------------------------
# (e) --help → rc=0, stdout contains 'Usage:'
# ---------------------------------------------------------------------------
def test_help_parity(tmp_path):
    home, db = _scenario(tmp_path)
    rb = _bash_metrics(home, db, ["--help"])
    assert rb.returncode == 0
    assert "Usage:" in rb.stdout
    # port
    import io
    out, err = io.StringIO(), io.StringIO()
    rc = pm.main(["--help"], stdout=out, stderr=err)
    assert rc == 0
    assert "Usage:" in out.getvalue()


# ---------------------------------------------------------------------------
# (f) unknown flag --bogus → rc=2, stderr contains 'Unknown flag'
# ---------------------------------------------------------------------------
def test_unknown_flag_parity(tmp_path):
    home, db = _scenario(tmp_path)
    rb = _bash_metrics(home, db, ["--bogus"])
    assert rb.returncode == 2
    assert "Unknown flag" in rb.stderr
    # port
    import io
    out, err = io.StringIO(), io.StringIO()
    rc = pm.main(["--bogus"], stdout=out, stderr=err)
    assert rc == 2
    assert "Unknown flag" in err.getvalue()


# ---------------------------------------------------------------------------
# (g) missing DB → rc=1, stderr contains 'no state.db at'
# ---------------------------------------------------------------------------
def test_missing_db_parity(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    nonexistent = str(home / "no-such.db")
    rb = _bash_metrics(home, nonexistent, [])
    assert rb.returncode == 1
    assert "no state.db at" in rb.stderr
    # port: feed MINI_ORK_DB via env, point main() at the same nonexistent path
    import io
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
# (h) --format json with 3 cycles → JSON byte-equal
# ---------------------------------------------------------------------------
def test_format_json_parity(tmp_path):
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    args = ["--since", str(PIN_DAY_BEFORE), "--format", "json"]
    rb = _bash_metrics(home, db, args)
    rp_data = pm.collect_cycles(db, recipe_filter="", since=PIN_DAY_BEFORE)
    rp_json = pm.render_json(rp_data)
    assert rb.returncode == 0
    assert rb.stdout == rp_json
    parsed = json.loads(rb.stdout)
    assert parsed["totals"]["cycle_count"] == 3


# ---------------------------------------------------------------------------
# (i) gradient_records missing-table parity → OperationalError branch (try/except)
# ---------------------------------------------------------------------------
def test_missing_gradient_table_parity(tmp_path):
    """Bash handles 'no such table: gradient_records' via try/except → 0.

    DROP the table to simulate a pre-0038 DB; both sides must report gradient_count=0.
    """
    home, db = _scenario(tmp_path)
    _seed_three_cycles(db)
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE gradient_records")
        con.commit()
    finally:
        con.close()

    args = ["--since", str(PIN_DAY_BEFORE), "--format", "json"]
    rb = _bash_metrics(home, db, args)
    assert rb.returncode == 0, rb.stderr
    rp_data = pm.collect_cycles(db, recipe_filter="", since=PIN_DAY_BEFORE)
    rp_json = pm.render_json(rp_data)
    assert rb.stdout == rp_json
    parsed = json.loads(rb.stdout)
    assert parsed["totals"]["gradient_count"] == 0
    assert parsed["totals"]["trace_count"] == 4