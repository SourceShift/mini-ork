"""Standalone unit tests for ``mini_ork.gates.gate_registry``.

Replaces the bash-parity gate (against ``lib/gate_registry.sh``) as part of
the bash→Python migration: the Python port is now the sole implementation,
so its coverage no longer runs the LIVE bash functions via
``bash -c 'source lib/gate_registry.sh; ...'`` — it asserts the port's
behaviour directly. The expected values below are the semantic contract
the bash side used to pin (verdicts, row shapes, error-path rejections),
now asserted on the port's output.

Cases (ten):

  (1) register id format — gate-<gtype[:6]>-<uuid4().hex[:8]> shape plus
      the stored row content for a known input
  (2) budget_gate pass — cost_usd=5.0 vs limit=10.0
  (3) budget_gate fail boundary — cost_usd=10.0 (pass) and 10.0000001
      (fail) — exercises the <= comparator at 1e-6 precision
  (4) human_gate always defers — constant 'defer' regardless of context
  (5) scope_gate pass and fail — task_class in/out of allowed list
  (6) evaluate unknown gate_id returns 'fail'
  (7) gate_list count and types — register 3 different gate_types and
      assert the sorted gate_type tuple
  (8) gate_run_all summary — aggregate verdict fields
  (9) invalid gate_type rejected — ValueError
  (10) missing required args rejected — TypeError
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT))
from mini_ork.gates import gate_registry as gr  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

# gate_id format: 'gate-<gtype[:6]>-<uuid4().hex[:8]>'. The suffix is
# random; we only assert the prefix shape.
_GATE_ID_RE = re.compile(r"^gate-(.{0,6})-([0-9a-f]{8})$")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — fresh temp DB per test, init via the Python port of db/init.sh.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """Initialise a fresh SQLite file via ``mini_ork.stores.migrate.init_db``.

    Sets MINI_ORK_HOME + MINI_ORK_DB in the test process so the port finds
    the right DB path. Lazy CREATE TABLE in the port handles the
    gate_registry table.
    """
    home = tmp_path
    db_path = home / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO_ROOT))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    # Make these visible to the Python port.
    import os
    os.environ["MINI_ORK_HOME"] = str(home)
    os.environ["MINI_ORK_DB"] = str(db_path)
    os.environ["MINI_ORK_ROOT"] = str(REPO_ROOT)
    return db_path


# ─────────────────────────────────────────────────────────────────────────────
# (1) register id format + row content
# ─────────────────────────────────────────────────────────────────────────────


def test_register_id_format_and_row_content(db):
    """gate_register returns a gate_id matching the documented format and
    stores the expected row content (gate_type, condition,
    task_class_filter, safety, active) for a known input."""
    gid = gr.gate_register(str(db), "budget_gate", "10.0", "write-code", safety=True)

    assert _GATE_ID_RE.match(gid), f"gid shape: {gid!r}"

    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT gate_type, condition, task_class_filter, safety, active "
            "FROM gate_registry ORDER BY gate_type, condition"
        ).fetchall()
    finally:
        con.close()

    assert rows == [("budget_gate", "10.0", "write-code", 1, 1)], rows


# ─────────────────────────────────────────────────────────────────────────────
# (2) budget_gate pass
# ─────────────────────────────────────────────────────────────────────────────


def test_budget_gate_pass(db):
    gid = gr.gate_register(str(db), "budget_gate", "10.0")
    v = gr.gate_evaluate(str(db), gid, '{"cost_usd": 5.0}')
    assert v == "pass", f"expected pass got {v!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (3) budget_gate boundary — 1e-6 tolerance case
# ─────────────────────────────────────────────────────────────────────────────


def test_budget_gate_boundary(db):
    gid = gr.gate_register(str(db), "budget_gate", "10.0")
    # Exactly at limit: <= comparison yields pass.
    at_limit = gr.gate_evaluate(str(db), gid, '{"cost_usd": 10.0}')
    assert at_limit == "pass", f"at-limit: {at_limit!r}"
    # Just over limit by 1e-6: must be fail.
    over_limit = gr.gate_evaluate(str(db), gid, '{"cost_usd": 10.0000001}')
    assert over_limit == "fail", f"over-limit: {over_limit!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (4) human_gate always defers
# ─────────────────────────────────────────────────────────────────────────────


def test_human_gate_always_defer(db):
    gid = gr.gate_register(str(db), "human_gate", "anything")
    for ctx in ('{}', '{"task_class":"x"}', '{"cost_usd":1.0}'):
        v = gr.gate_evaluate(str(db), gid, ctx)
        assert v == "defer", f"defer failed for ctx={ctx}: {v!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (5) scope_gate pass and fail
# ─────────────────────────────────────────────────────────────────────────────


def test_scope_gate_pass_and_fail(db):
    gid = gr.gate_register(str(db), "scope_gate", '["a","b"]')
    pass_v = gr.gate_evaluate(str(db), gid, '{"task_class":"a"}')
    fail_v = gr.gate_evaluate(str(db), gid, '{"task_class":"z"}')
    assert pass_v == "pass"
    assert fail_v == "fail"


# ─────────────────────────────────────────────────────────────────────────────
# (6) unknown gate_id returns 'fail'
# ─────────────────────────────────────────────────────────────────────────────


def test_evaluate_unknown_gate_returns_fail(db):
    v = gr.gate_evaluate(str(db), "gate-zzzz-00000000", '{}')
    assert v == "fail", f"unknown: {v!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (7) gate_list — count and sorted gate_type tuple
# ─────────────────────────────────────────────────────────────────────────────


def test_gate_list_count_and_types(db):
    gr.gate_register(str(db), "budget_gate", "10.0")
    gr.gate_register(str(db), "human_gate", "x")
    gr.gate_register(str(db), "scope_gate", '["t"]')

    gates = gr.gate_list(str(db))

    assert len(gates) == 3
    types = sorted(g["gate_type"] for g in gates)
    assert types == ["budget_gate", "human_gate", "scope_gate"], types


# ─────────────────────────────────────────────────────────────────────────────
# (8) gate_run_all summary
# ─────────────────────────────────────────────────────────────────────────────


def test_run_all_summary(db):
    gr.gate_register(str(db), "budget_gate", "20.0", task_class_filter="tc")
    gr.gate_register(str(db), "scope_gate", '["tc"]', task_class_filter="tc")

    ctx = '{"task_class":"tc","cost_usd":5.0}'
    summary = gr.gate_run_all(str(db), "tc", ctx)

    assert summary["all_pass"] is True
    assert summary["any_defer"] is False
    assert summary["safety_violation"] is False
    assert summary["gate_count"] == 2
    verdicts = sorted(g["verdict"] for g in summary["gates"])
    assert verdicts == ["pass", "pass"]


# ─────────────────────────────────────────────────────────────────────────────
# (9) invalid gate_type rejected
# ─────────────────────────────────────────────────────────────────────────────


def test_register_invalid_gate_type_rejected(db):
    """An unknown gate_type is rejected: the Python port raises ValueError.
    Subsumes the retired gate_registry bash fixture's 'invalid gate_type
    exits non-zero' case."""
    with pytest.raises(ValueError):
        gr.gate_register(str(db), "invalid_gate_type", "condition")


# ─────────────────────────────────────────────────────────────────────────────
# (10) missing required args rejected
# ─────────────────────────────────────────────────────────────────────────────


def test_register_missing_args_rejected(db):
    """Calling gate_register with no gate_type/condition is rejected: the
    Python port raises TypeError for the missing required positional
    arguments. Subsumes the retired gate_registry bash fixture's 'no args
    exits non-zero' case."""
    with pytest.raises(TypeError):
        gr.gate_register(str(db))  # type: ignore[call-arg]
