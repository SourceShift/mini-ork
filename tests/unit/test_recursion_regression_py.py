"""I8a: a recursion loop stops when a node that passed in an earlier round fails.

Two layers, both driven through the real executor:

* ``regressed_sources`` — the pure set helper that names a wave's regressions.
* the round-granting guard in ``mini_ork/cli/execute.py`` — a recursion-shaped
  DAG whose ``test`` verifier passes round 0 (``pass_first``) and fails round 1
  must stop for the implementer target, print the reason, and record a run note,
  instead of granting round 2 off the regressed state.

The control cases pin today's behaviour when the guard must stay out of the way:
without a ``recursion:`` block the escalation edges are inert (no round, no
message), and a verifier that never passed is not a regression.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mini_ork.workflow.recursion import regressed_sources  # noqa: E402
import test_revise_loop as revise_loop  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_run_environment(monkeypatch):
    """Same env contract as ``test_revise_loop``; the shared ``_run`` sets the
    rest explicitly, but a stale var in the caller's shell must not leak in."""
    for name in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_PLAN_PATH",
        "MINI_ORK_RUN_ID", "MO_TARGET_CWD", "MO_APPLY_IMPL_OUTPUT",
        "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE_ROOT", "MO_REVISE_ROUNDS",
        "MO_REVISE_TYPECHECK_BEHAVIOR", "MO_REVISE_TEST_BEHAVIOR",
        "MO_GRADE_RUN_REWARD", "MO_LEARNING_WRITEBACK", "MO_LANE_ROUTER",
    ):
        monkeypatch.delenv(name, raising=False)


# ── the pure helper ──────────────────────────────────────────────────────────

def test_regressed_sources_hits_a_pass_that_now_fails():
    assert regressed_sources({"test"}, ["test"]) == ["test"]


def test_regressed_sources_is_empty_without_earlier_passes():
    assert regressed_sources(set(), ["test"]) == []


def test_regressed_sources_ignores_a_disjoint_failure():
    assert regressed_sources({"typecheck"}, ["test"]) == []


def test_regressed_sources_is_order_independent_and_de_duplicated():
    assert regressed_sources({"a", "b"}, ["b", "a", "b"]) == ["a", "b"]


# ── wiring: the regression guard inside the round-granting loop ──────────────

def _regression_workflow(*, with_recursion=False) -> dict:
    """A recursion-shaped DAG mirroring ``_escalation_workflow`` but with the
    ``test`` verifier as the repair source.

    ``test -> implementer`` and ``reviewer -> implementer`` are ``escalates_to``
    edges. They stay inert unless the workflow declares a ``recursion:`` block,
    which is what makes ``_escalation_fired`` true and brings the guard into
    play. ``dispatch_mode: serial`` puts each node in its own wave so the
    pass-then-fail sequence of the ``test`` verifier is deterministic.
    """
    workflow = {
        "version": "0.2.0",
        "task_class": "code_fix",
        "dispatch_mode": "serial",
        "nodes": [
            {"name": "planner", "type": "planner", "dispatch_mode": "serial"},
            {"name": "implementer", "type": "implementer",
             "model_lane": "worker", "dispatch_mode": "serial"},
            {"name": "test", "type": "verifier",
             "verifier_ref": "verifiers/test.py", "dispatch_mode": "serial"},
            {"name": "reviewer", "type": "reviewer",
             "model_lane": "reviewer", "dispatch_mode": "serial"},
        ],
        "edges": [
            {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "test", "edge_type": "verifies"},
            {"from": "test", "to": "reviewer", "edge_type": "depends_on"},
            {"from": "test", "to": "implementer", "edge_type": "escalates_to"},
            {"from": "reviewer", "to": "implementer", "edge_type": "escalates_to"},
        ],
    }
    if with_recursion:
        workflow["recursion"] = {
            "max_iterations": 3,
            "budget_cap_total_usd": 100.0,
        }
    return workflow


def _run_notes(tmp_path: Path) -> str:
    db = tmp_path / "db" / ".mini-ork" / "state.db"
    with sqlite3.connect(str(db)) as con:
        row = con.execute(
            "SELECT notes FROM task_runs WHERE id = 'r'").fetchone()
    return (row[0] if row and row[0] else "") or ""


def _revise_rounds(run_dir: Path) -> list[int]:
    revise = run_dir / "revise"
    if not revise.is_dir():
        return []
    return sorted(
        int(p.stem.split("-", 1)[1])
        for p in revise.glob("round-*.md")
        if p.stem.split("-", 1)[1].isdigit())


def test_recursion_regression_stops_the_loop(tmp_path, monkeypatch, capsys):
    """test passes round 0, the reviewer fails and grants round 1; the verifier
    now fails. That is a regression of a source that passed in an earlier wave,
    so the loop stops for the implementer and says why."""
    rc, state, run_dir = revise_loop._run(
        tmp_path, monkeypatch,
        workflow=_regression_workflow(with_recursion=True),
        reviewer_verdicts=["needs_revision", "pass"],
        test="pass_first",
    )
    err = capsys.readouterr().err

    assert "recursion regression" in err
    assert "test passed in an earlier round and now fail" in err
    assert "recursion_regression: implementer (test)" in _run_notes(tmp_path)

    # Round 0 + the granted revise round 1. The regressed wave must NOT grant a
    # second revise round, so the implementer is not re-dispatched afterwards.
    assert state["implementer"] == 2
    assert _revise_rounds(run_dir) == [1]
    assert rc != 0


def test_without_a_recursion_block_behaves_as_today(tmp_path, monkeypatch, capsys):
    """The opt-out is the declaration: the same DAG with no ``recursion:`` block
    keeps ``escalates_to`` inert, so there is no repair round and no message."""
    rc, state, run_dir = revise_loop._run(
        tmp_path, monkeypatch,
        workflow=_regression_workflow(with_recursion=False),
        reviewer_verdicts=["needs_revision", "pass"],
        test="pass_first",
    )
    err = capsys.readouterr().err

    assert "recursion regression" not in err
    assert state["implementer"] == 1          # no revise round, today's count
    assert _revise_rounds(run_dir) == []
    assert rc != 0


def test_a_verifier_that_never_passed_is_not_a_regression(tmp_path, monkeypatch, capsys):
    """A source that only ever failed never regressed: the guard must not stop
    the loop, which is what lets an ordinary recursion loop keep iterating."""
    rc, state, _run_dir = revise_loop._run(
        tmp_path, monkeypatch,
        workflow=_regression_workflow(with_recursion=True),
        reviewer_verdicts=["needs_revision"],
        test="fail",
    )
    err = capsys.readouterr().err

    assert "recursion regression" not in err
    assert state["implementer"] > 1           # the loop did iterate
    assert rc != 0
