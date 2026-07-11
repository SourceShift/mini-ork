"""Parity gate: mini_ork.ported.active_state_index vs lib/active_state_index.sh.

Eight cases (kickoff floor: >=6; 2-case buffer):

  (a) empty DB                  → block with empty unresolved/open/facts/goals
                                   + 6 decision_variables + Summary absent
  (b) failure_memory row        → unresolved_errors surfaces that row
  (c) policy_decisions DENY     → open_constraints surfaces DENY row
  (d) policy_decisions REQ_APP  → open_constraints surfaces REQUIRE_APPROVAL
  (e) task_runs APPROVE         → established_facts surfaces the row,
                                   cost_usd/duration_ms floats 1e-6
  (f) task_runs executing       → pending_goals surfaces the in-flight run
  (g) MO_DISABLE_ACTIVE_STATE=1 → both sides emit literal empty string
  (h) task_class filter         → "code_fix" arg excludes "audit" row,
                                   includes matching rows

Comparison strategy:
  - bash subprocess stdout captured verbatim (includes trailing \n from
    bash's last print()). Python port returns string with trailing \n.
  - Extract the embedded JSON via regex; parse both sides; compare
    dict-by-dict with math.isclose abs_tol=1e-6 for cost_usd/duration_ms,
    exact equality for everything else.
  - Markdown wrapper (header + closing fence + Summary line) compared
    byte-equal after stripping the JSON content to a placeholder.

No mocks, no hardcoded outputs beyond the structural fixtures both
sides consume. Every assertion is bash-subprocess-vs-Python-port on
identical inputs.
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
from mini_ork.ported import active_state_index as asi  # noqa: E402

SH = REPO / "lib" / "active_state_index.sh"
INIT_SH = REPO / "db" / "init.sh"

_FLOAT_TOL = 1e-6
_FLOAT_KEYS = {"cost_usd", "duration_ms"}

# Extracts the JSON object between ```json ... ``` fences (DOTALL for
# multi-line JSON body).
_JSON_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which(*tools: str) -> None:
    for t in tools:
        if not shutil.which(t):
            pytest.skip(f"required tool not on PATH: {t}")
    if not SH.exists():
        pytest.skip(f"missing lib/active_state_index.sh at {SH}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


def _source_and_call(fn_call: str, db_path: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Source lib/active_state_index.sh and invoke ``fn_call``.

    MINI_ORK_DB is set to db_path. ``env_extra`` layers on top of
    os.environ (used for MO_DISABLE_ACTIVE_STATE=1 case).
    """
    bash_src = (
        f'. "{SH}" >/dev/null 2>&1\n'
        f'{fn_call}\n'
    )
    env = {**os.environ, "MINI_ORK_DB": db_path}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", bash_src],
        capture_output=True,
        text=True,
        env=env,
    )


def _extract_json(block: str) -> dict:
    m = _JSON_RE.search(block)
    if not m:
        raise AssertionError(f"no ```json``` block found in:\n{block!r}")
    return json.loads(m.group(1))


def _strip_json(block: str) -> str:
    """Replace JSON body with a placeholder so we can compare wrapper byte-equal."""
    return _JSON_RE.sub("```json\n<X>\n```", block)


def _compare_blocks(bash_block: str, py_block: str) -> None:
    """Deep-compare bash vs Python output.

    Markdown wrapper (excluding JSON body) must be byte-equal. JSON
    content compared field-by-field with 1e-6 float tolerance for
    cost_usd/duration_ms.
    """
    # Wrapper byte-equal (after stripping the variable JSON body)
    bash_wrap = _strip_json(bash_block)
    py_wrap = _strip_json(py_block)
    assert bash_wrap == py_wrap, (
        f"markdown wrapper byte-mismatch:\nbash={bash_wrap!r}\npy  ={py_wrap!r}"
    )

    # JSON content: dict-by-dict
    bash_j = _extract_json(bash_block)
    py_j = _extract_json(py_block)

    # Top-level scalars
    for key in ("schema", "source"):
        assert bash_j.get(key) == py_j.get(key), (
            f"top-level {key}: bash={bash_j.get(key)!r} py={py_j.get(key)!r}"
        )

    # decision_variables exact
    assert bash_j["decision_variables"] == py_j["decision_variables"], (
        f"decision_variables differ:\nbash={bash_j['decision_variables']!r}\n"
        f"py  ={py_j['decision_variables']!r}"
    )

    # 4 list sections — same length, field-by-field with float tolerance
    for section in ("unresolved_errors", "open_constraints", "established_facts", "pending_goals"):
        b_list = bash_j[section]
        p_list = py_j[section]
        assert len(b_list) == len(p_list), (
            f"{section} length mismatch: bash={len(b_list)} py={len(p_list)}"
        )
        for i, (b_row, p_row) in enumerate(zip(b_list, p_list)):
            assert set(b_row.keys()) == set(p_row.keys()), (
                f"{section}[{i}] keys differ:\n"
                f"bash={sorted(b_row.keys())}\npy  ={sorted(p_row.keys())}"
            )
            for k in b_row:
                bv, pv = b_row[k], p_row[k]
                if k in _FLOAT_KEYS:
                    if bv is None and pv is None:
                        continue
                    if bv is None or pv is None:
                        assert bv == pv, (
                            f"{section}[{i}].{k} None mismatch: bash={bv!r} py={pv!r}"
                        )
                        continue
                    assert math.isclose(float(bv), float(pv), abs_tol=_FLOAT_TOL), (
                        f"{section}[{i}].{k} drift: bash={bv} py={pv} tol={_FLOAT_TOL}"
                    )
                else:
                    assert bv == pv, (
                        f"{section}[{i}].{k} mismatch:\nbash={bv!r}\npy  ={pv!r}"
                    )


# ─────────────────────────────────────────────────────────────────────────────
# DB scaffold fixture (real db/init.sh against tmp_path)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path):
    """Spin up a real mini-ork SQLite DB via db/init.sh with a unique
    path per test. Applies the full migration graph (0001..N) including
    0009_memory_namespaces, 0013_task_runs, 0026_policy_state which
    supply failure_memory / task_runs / policy_decisions."""
    _which("bash", "python3", "sqlite3")
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
        # the bash self-test stubs a runs row when seeding. Match that.
        con.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY)")
        con.execute("INSERT OR IGNORE INTO runs (id) VALUES (?)", (run_id,))
        con.execute(
            "INSERT INTO failure_memory (failure_id, run_id, workflow_stage, "
            "failure_category, error_message) VALUES (?, ?, 'reviewer', "
            "'verifier_fail', 'parity-test fixture')",
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
            "'constraint_safety', ?, ?, 'parity-test fixture')",
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
def test_empty_db_parity(temp_db):
    """Fresh DB via db/init.sh has 0 rows in failure_memory/policy_decisions/
    task_runs. Both sides should print a block with all 4 list sections
    empty + decision_variables populated. The Summary line should be
    absent because counts is empty."""
    _which("bash", "python3")

    rb = _source_and_call('mo_active_state_block "__any__" 30', temp_db)
    assert rb.returncode == 0, f"bash rc={rb.returncode}: {rb.stderr}"
    bash_out = rb.stdout

    py_out = asi.render_active_state_block(task_class="__any__", days_window=30, db_path=temp_db)

    _compare_blocks(bash_out, py_out)
    # Sanity: empty DB still has decision_variables (6 entries).
    bash_j = _extract_json(bash_out)
    assert len(bash_j["decision_variables"]) == 6
    # And the 4 list sections are empty.
    for s in ("unresolved_errors", "open_constraints", "established_facts", "pending_goals"):
        assert bash_j[s] == [], f"{s} should be empty on fresh DB"
    # No Summary line when counts empty.
    assert "**Summary:**" not in bash_out, "Summary line should be absent on empty DB"


# ─────────────────────────────────────────────────────────────────────────────
# (b) failure_memory row surfaces as unresolved_error
# ─────────────────────────────────────────────────────────────────────────────
def test_failure_memory_parity(temp_db):
    _which("bash", "python3")
    _seed_failure_memory(temp_db, "fm-parity-1")

    rb = _source_and_call('mo_active_state_block "__any__" 30', temp_db)
    assert rb.returncode == 0, f"bash rc={rb.returncode}: {rb.stderr}"
    py_out = asi.render_active_state_block(task_class="__any__", days_window=30, db_path=temp_db)

    _compare_blocks(rb.stdout, py_out)
    bash_j = _extract_json(rb.stdout)
    assert any(r["failure_id"] == "fm-parity-1" for r in bash_j["unresolved_errors"]), (
        f"fm-parity-1 missing from unresolved_errors:\n{bash_j['unresolved_errors']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) policy_decisions DENY row surfaces as open_constraint
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_decisions_deny_parity(temp_db):
    _which("bash", "python3")
    _seed_policy_decision(temp_db, "pd-parity-deny", "DENY")

    rb = _source_and_call('mo_active_state_block "__any__" 30', temp_db)
    assert rb.returncode == 0, f"bash rc={rb.returncode}: {rb.stderr}"
    py_out = asi.render_active_state_block(task_class="__any__", days_window=30, db_path=temp_db)

    _compare_blocks(rb.stdout, py_out)
    bash_j = _extract_json(rb.stdout)
    matches = [r for r in bash_j["open_constraints"] if r["decision_id"] == "pd-parity-deny"]
    assert matches, f"pd-parity-deny missing from open_constraints:\n{bash_j['open_constraints']!r}"
    assert matches[0]["result"] == "DENY"


# ─────────────────────────────────────────────────────────────────────────────
# (d) policy_decisions REQUIRE_APPROVAL row surfaces as open_constraint
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_decisions_require_approval_parity(temp_db):
    _which("bash", "python3")
    _seed_policy_decision(
        temp_db, "pd-parity-reqapp", "REQUIRE_APPROVAL",
        policy_name="per_run_cost_cap",
    )

    rb = _source_and_call('mo_active_state_block "__any__" 30', temp_db)
    assert rb.returncode == 0, f"bash rc={rb.returncode}: {rb.stderr}"
    py_out = asi.render_active_state_block(task_class="__any__", days_window=30, db_path=temp_db)

    _compare_blocks(rb.stdout, py_out)
    bash_j = _extract_json(rb.stdout)
    matches = [r for r in bash_j["open_constraints"] if r["decision_id"] == "pd-parity-reqapp"]
    assert matches, f"pd-parity-reqapp missing from open_constraints:\n{bash_j['open_constraints']!r}"
    assert matches[0]["result"] == "REQUIRE_APPROVAL"


# ─────────────────────────────────────────────────────────────────────────────
# (e) task_runs APPROVE → established_facts (with float fields)
# ─────────────────────────────────────────────────────────────────────────────
def test_established_facts_parity(temp_db):
    _which("bash", "python3")
    _seed_task_run(
        temp_db,
        run_id="run-parity-est",
        task_class="code_fix",
        status="published",
        verdict="APPROVE",
        cost_usd=0.123456789,
        duration_ms=4321,
    )

    rb = _source_and_call('mo_active_state_block "code_fix" 30', temp_db)
    assert rb.returncode == 0, f"bash rc={rb.returncode}: {rb.stderr}"
    py_out = asi.render_active_state_block(task_class="code_fix", days_window=30, db_path=temp_db)

    _compare_blocks(rb.stdout, py_out)
    bash_j = _extract_json(rb.stdout)
    matches = [r for r in bash_j["established_facts"] if r["run_id"] == "run-parity-est"]
    assert matches, f"run-parity-est missing from established_facts:\n{bash_j['established_facts']!r}"
    # Floats within 1e-6 (already enforced in _compare_blocks, sanity here).
    assert math.isclose(float(matches[0]["cost_usd"]), 0.123456789, abs_tol=_FLOAT_TOL)
    assert int(matches[0]["duration_ms"]) == 4321


# ─────────────────────────────────────────────────────────────────────────────
# (f) task_runs executing → pending_goals
# ─────────────────────────────────────────────────────────────────────────────
def test_pending_goals_parity(temp_db):
    _which("bash", "python3")
    _seed_task_run(
        temp_db,
        run_id="run-parity-pend",
        task_class="code_fix",
        status="executing",
    )

    rb = _source_and_call('mo_active_state_block "code_fix" 30', temp_db)
    assert rb.returncode == 0, f"bash rc={rb.returncode}: {rb.stderr}"
    py_out = asi.render_active_state_block(task_class="code_fix", days_window=30, db_path=temp_db)

    _compare_blocks(rb.stdout, py_out)
    bash_j = _extract_json(rb.stdout)
    matches = [r for r in bash_j["pending_goals"] if r["run_id"] == "run-parity-pend"]
    assert matches, f"run-parity-pend missing from pending_goals:\n{bash_j['pending_goals']!r}"
    assert matches[0]["status"] == "executing"


# ─────────────────────────────────────────────────────────────────────────────
# (g) MO_DISABLE_ACTIVE_STATE=1 — both sides emit empty string
# ─────────────────────────────────────────────────────────────────────────────
def test_disable_flag_short_circuit_parity(temp_db):
    _which("bash", "python3")
    _seed_task_run(
        temp_db, "run-should-not-appear", "code_fix", "executing",
    )

    rb = _source_and_call(
        'mo_active_state_block "code_fix" 30',
        temp_db,
        env_extra={"MO_DISABLE_ACTIVE_STATE": "1"},
    )
    assert rb.returncode == 0, f"bash rc={rb.returncode}: {rb.stderr}"
    py_out = asi.render_active_state_block(
        task_class="code_fix",
        days_window=30,
        db_path=temp_db,
        disabled=True,
    )

    assert rb.stdout == "", f"bash disabled: expected empty, got {rb.stdout!r}"
    assert py_out == "", f"python disabled: expected empty, got {py_out!r}"
    # Byte-equal empty output
    assert rb.stdout == py_out


# ─────────────────────────────────────────────────────────────────────────────
# (h) task_class filter — code_fix excludes audit row
# ─────────────────────────────────────────────────────────────────────────────
def test_task_class_filter_parity(temp_db):
    _which("bash", "python3")
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

    rb = _source_and_call('mo_active_state_block "code_fix" 30', temp_db)
    assert rb.returncode == 0, f"bash rc={rb.returncode}: {rb.stderr}"
    py_out = asi.render_active_state_block(task_class="code_fix", days_window=30, db_path=temp_db)

    _compare_blocks(rb.stdout, py_out)
    bash_j = _extract_json(rb.stdout)
    fact_ids = [r["run_id"] for r in bash_j["established_facts"]]
    assert "run-parity-codefix" in fact_ids, (
        f"code_fix run missing from established_facts:\n{fact_ids!r}"
    )
    assert "run-parity-audit" not in fact_ids, (
        f"audit run leaked into code_fix-filtered established_facts:\n{fact_ids!r}"
    )