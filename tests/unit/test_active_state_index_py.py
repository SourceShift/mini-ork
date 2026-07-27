"""Unit tests: mini_ork.orchestration.active_state_index (bash parity halves removed; formerly vs lib/active_state_index.sh).

Eight cases:

  (a) empty DB                  → block with empty unresolved/open/facts/goals
                                   + 6 decision_variables + Summary absent
  (b) failure_memory row        → unresolved_errors surfaces that row
  (c) policy_decisions DENY     → open_constraints surfaces DENY row
  (d) policy_decisions REQ_APP  → open_constraints surfaces REQUIRE_APPROVAL
  (e) task_runs APPROVE         → established_facts surfaces the row,
                                   cost_usd/duration_ms floats 1e-6
  (f) task_runs executing       → pending_goals surfaces the in-flight run
  (g) MO_DISABLE_ACTIVE_STATE=1 → literal empty string
  (h) task_class filter         → "code_fix" arg excludes "audit" row,
                                   includes matching rows

Output contract: a markdown wrapper with an embedded ```json ...``` block;
the JSON has top-level ``schema``/``source`` keys, a 6-entry
``decision_variables`` list, and the four list sections above.
"""
from __future__ import annotations

import json
import math
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
from mini_ork.orchestration import active_state_index as asi

INIT_SH = REPO / "db" / "init.sh"

_FLOAT_TOL = 1e-6

# Extracts the JSON object between ```json ... ``` fences (DOTALL for
# multi-line JSON body).
_JSON_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _extract_json(block: str) -> dict:
    m = _JSON_RE.search(block)
    if not m:
        raise AssertionError(f"no ```json``` block found in:\n{block!r}")
    return json.loads(m.group(1))


# ─────────────────────────────────────────────────────────────────────────────
# DB scaffold fixture (real db/init.sh against tmp_path)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path):
    """Spin up a real mini-ork SQLite DB via db/init.sh with a unique
    path per test. Applies the full migration graph (0001..N) including
    0009_memory_namespaces, 0013_task_runs, 0026_policy_state which
    supply failure_memory / task_runs / policy_decisions."""
    for t in ("bash", "sqlite3"):
        if not shutil.which(t):
            pytest.skip(f"required tool not on PATH: {t}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: {r.stderr}\n{r.stdout}")
    return dbp


def _seed_failure_memory(db_path: str, failure_id: str, run_id: int = 1) -> None:
    con = sqlite3.connect(db_path)
    try:
        # failure_memory FK may reference runs (legacy) or be self-contained;
        # stub a runs row when seeding.
        con.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY)")
        con.execute("INSERT OR IGNORE INTO runs (id) VALUES (?)", (run_id,))
        con.execute(
            "INSERT INTO failure_memory (failure_id, run_id, workflow_stage, "
            "failure_category, error_message) VALUES (?, ?, 'reviewer', "
            "'verifier_fail', 'unit-test fixture')",
            (failure_id, run_id),
        )
        con.commit()
    finally:
        con.close()


def _seed_policy_decision(
    db_path: str, decision_id: str, result: str, policy_name: str = "no_unsandboxed_dispatch",
) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO policy_decisions (decision_id, run_id, event_type, "
            "policy_name, result, reason) VALUES (?, 'run-x', "
            "'constraint_safety', ?, ?, 'unit-test fixture')",
            (decision_id, policy_name, result),
        )
        con.commit()
    finally:
        con.close()


def _seed_task_run(
    db_path: str,
    run_id: str,
    task_class: str,
    status: str,
    verdict: str | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
) -> None:
    con = sqlite3.connect(db_path)
    try:
        now = "strftime('%s','now')"
        cols = ["id", "task_class", "recipe", "kickoff_path", "status",
                "created_at", "updated_at"]
        vals: list = [run_id, task_class, "code-fix", "/tmp/k.md", status,
                      f"{now}", f"{now}"]
        if verdict is not None:
            cols.append("verdict")
            vals.append(verdict)
        if cost_usd is not None:
            cols.append("cost_usd")
            vals.append(cost_usd)
        if duration_ms is not None:
            cols.append("duration_ms")
            vals.append(duration_ms)
        if status in ("published",) and verdict == "APPROVE":
            cols.append("ended_at")
            vals.append(f"{now} - 3000")
        placeholders = ",".join("?" for _ in vals)
        con.execute(
            f"INSERT INTO task_runs ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) empty DB — block prints with decision_variables only, no Summary line
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_db(temp_db):
    """Fresh DB via db/init.sh has 0 rows in failure_memory/policy_decisions/
    task_runs. The block has all 4 list sections empty + decision_variables
    populated. The Summary line is absent because counts is empty."""
    py_out = asi.render_active_state_block(task_class="__any__", days_window=30, db_path=temp_db)

    py_j = _extract_json(py_out)
    # decision_variables populated (6 entries).
    assert len(py_j["decision_variables"]) == 6
    # top-level scalar keys present
    assert "schema" in py_j and "source" in py_j
    # the 4 list sections are empty.
    for s in ("unresolved_errors", "open_constraints", "established_facts", "pending_goals"):
        assert py_j[s] == [], f"{s} should be empty on fresh DB"
    # No Summary line when counts empty.
    assert "**Summary:**" not in py_out, "Summary line should be absent on empty DB"


# ─────────────────────────────────────────────────────────────────────────────
# (b) failure_memory row surfaces as unresolved_error
# ─────────────────────────────────────────────────────────────────────────────
def test_failure_memory(temp_db):
    _seed_failure_memory(temp_db, "fm-parity-1")

    py_out = asi.render_active_state_block(task_class="__any__", days_window=30, db_path=temp_db)

    py_j = _extract_json(py_out)
    assert any(r["failure_id"] == "fm-parity-1" for r in py_j["unresolved_errors"]), (
        f"fm-parity-1 missing from unresolved_errors:\n{py_j['unresolved_errors']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) policy_decisions DENY row surfaces as open_constraint
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_decisions_deny(temp_db):
    _seed_policy_decision(temp_db, "pd-parity-deny", "DENY")

    py_out = asi.render_active_state_block(task_class="__any__", days_window=30, db_path=temp_db)

    py_j = _extract_json(py_out)
    matches = [r for r in py_j["open_constraints"] if r["decision_id"] == "pd-parity-deny"]
    assert matches, f"pd-parity-deny missing from open_constraints:\n{py_j['open_constraints']!r}"
    assert matches[0]["result"] == "DENY"


# ─────────────────────────────────────────────────────────────────────────────
# (d) policy_decisions REQUIRE_APPROVAL row surfaces as open_constraint
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_decisions_require_approval(temp_db):
    _seed_policy_decision(
        temp_db, "pd-parity-reqapp", "REQUIRE_APPROVAL",
        policy_name="per_run_cost_cap",
    )

    py_out = asi.render_active_state_block(task_class="__any__", days_window=30, db_path=temp_db)

    py_j = _extract_json(py_out)
    matches = [r for r in py_j["open_constraints"] if r["decision_id"] == "pd-parity-reqapp"]
    assert matches, f"pd-parity-reqapp missing from open_constraints:\n{py_j['open_constraints']!r}"
    assert matches[0]["result"] == "REQUIRE_APPROVAL"


# ─────────────────────────────────────────────────────────────────────────────
# (e) task_runs APPROVE → established_facts (with float fields)
# ─────────────────────────────────────────────────────────────────────────────
def test_established_facts(temp_db):
    _seed_task_run(
        temp_db,
        run_id="run-parity-est",
        task_class="code_fix",
        status="published",
        verdict="APPROVE",
        cost_usd=0.123456789,
        duration_ms=4321,
    )

    py_out = asi.render_active_state_block(task_class="code_fix", days_window=30, db_path=temp_db)

    py_j = _extract_json(py_out)
    matches = [r for r in py_j["established_facts"] if r["run_id"] == "run-parity-est"]
    assert matches, f"run-parity-est missing from established_facts:\n{py_j['established_facts']!r}"
    assert math.isclose(float(matches[0]["cost_usd"]), 0.123456789, abs_tol=_FLOAT_TOL)
    assert int(matches[0]["duration_ms"]) == 4321


# ─────────────────────────────────────────────────────────────────────────────
# (f) task_runs executing → pending_goals
# ─────────────────────────────────────────────────────────────────────────────
def test_pending_goals(temp_db):
    _seed_task_run(
        temp_db,
        run_id="run-parity-pend",
        task_class="code_fix",
        status="executing",
    )

    py_out = asi.render_active_state_block(task_class="code_fix", days_window=30, db_path=temp_db)

    py_j = _extract_json(py_out)
    matches = [r for r in py_j["pending_goals"] if r["run_id"] == "run-parity-pend"]
    assert matches, f"run-parity-pend missing from pending_goals:\n{py_j['pending_goals']!r}"
    assert matches[0]["status"] == "executing"


# ─────────────────────────────────────────────────────────────────────────────
# (g) MO_DISABLE_ACTIVE_STATE=1 — literal empty string
# ─────────────────────────────────────────────────────────────────────────────
def test_disable_flag_short_circuit(temp_db):
    _seed_task_run(
        temp_db, "run-should-not-appear", "code_fix", "executing",
    )

    py_out = asi.render_active_state_block(
        task_class="code_fix",
        days_window=30,
        db_path=temp_db,
        disabled=True,
    )

    assert py_out == "", f"disabled: expected empty, got {py_out!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (h) task_class filter — code_fix excludes audit row
# ─────────────────────────────────────────────────────────────────────────────
def test_task_class_filter(temp_db):
    _seed_task_run(
        temp_db,
        run_id="run-parity-codefix",
        task_class="code_fix",
        status="published",
        verdict="APPROVE",
        cost_usd=0.5,
        duration_ms=1000,
    )
    _seed_task_run(
        db_path=temp_db,
        run_id="run-parity-audit",
        task_class="audit",
        status="published",
        verdict="APPROVE",
        cost_usd=0.25,
        duration_ms=500,
    )

    py_out = asi.render_active_state_block(task_class="code_fix", days_window=30, db_path=temp_db)

    py_j = _extract_json(py_out)
    fact_ids = [r["run_id"] for r in py_j["established_facts"]]
    assert "run-parity-codefix" in fact_ids, (
        f"code_fix run missing from established_facts:\n{fact_ids!r}"
    )
    assert "run-parity-audit" not in fact_ids, (
        f"audit run leaked into code_fix-filtered established_facts:\n{fact_ids!r}"
    )
