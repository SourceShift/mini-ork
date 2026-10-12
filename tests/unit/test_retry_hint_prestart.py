"""``retry_hint`` — the run that died BEFORE any node ran.

Four audit findings (2026-10-12) over the failed runs of the previous day:

  * a run the operator killed reported "the cause was not classified" — a red
    row with no story for work a person deliberately stopped (case 1.7,
    ``stopped``);
  * a run whose plan was built and then never executed (the dispatcher reaped
    between planning and the first ``node_start``) also read as unclassified,
    although the plan was already paid for and resuming at the first node is
    the cheap fix (case 4.5, ``not-started``);
  * a *structured* verifier reports its failure as a list of absent keys
    (``missing``), and the reason reader only looked at prose keys, so the run
    classified as unclassified while its own artefact held the exact list;
  * a run whose recipe cannot be resolved (a per-run synthesised workflow, a
    deleted engine worktree) made every node-keyed artefact lookup blind and
    the hint said "Failed at ?" — although ``run_events`` names every node.

Fixtures mirror ``tests/unit/test_retry_hint_interrupted.py``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mini_ork.recovery import retry_hint
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-rhp-1791749297-c51101"
NOW = 1_791_749_297


WORKFLOW = """\
version: 1
task_class: framework_edit
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: live_smoke, type: verifier, verifier_ref: lib/live_smoke.py}
  - {name: static_check, type: verifier, verifier_ref: verifiers/static-check.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: live_smoke, edge_type: depends_on}
  - {from: live_smoke, to: static_check, edge_type: depends_on}
  - {from: static_check, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
"""


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "framework-edit"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: framework_edit\ndescription: rh\n")
    return h


def _seed_run(home: Path, run_id: str = RUN, *, recipe: str = "framework-edit",
              notes: str = "", status: str = "failed") -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# prestart test\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.5, NOW, NOW + 100, NOW + 80,
         "framework_edit", str(kickoff), "latest", notes or None))
    con.commit()
    con.close()
    return run_dir


def _insert_events(home: Path, run_id: str,
                   events: list[tuple[str, dict[str, Any]]], *,
                   start: int = NOW + 10) -> None:
    con = sqlite3.connect(home / "state.db")
    for i, (et, payload) in enumerate(events):
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, "
            "created_at) VALUES (?,?,?,?,?)",
            (f"ev-{run_id}-{i:02d}", run_id, et, json.dumps(payload), start + i))
    con.commit()
    con.close()


def _write_verifier(run_dir: Path, stem: str, payload: dict[str, Any]) -> None:
    (run_dir / f"verifier_{stem}.json").write_text(json.dumps(payload), encoding="utf-8")


# ── case 1.7 — stopped by the operator ─────────────────────────────────────


def test_killed_by_user_is_stopped_not_unclassified(home: Path) -> None:
    """``board kill`` writes ``killed-by-user``; the hint must say so."""
    _seed_run(home, notes="killed-by-user")
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "stopped"
    assert hint["retryable"] is False
    assert hint["strategy"] == "none"
    assert "operator" in hint["needs_change"]["summary"].lower()


def test_killed_by_user_is_found_at_the_end_of_the_notes(home: Path) -> None:
    """Notes are append-only: an earlier note must not push the marker out."""
    _seed_run(home, notes="planned; killed-by-user")
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "stopped"


# ── case 4.5 — planned, but no node ever started ───────────────────────────


def test_planned_but_no_node_ran_resumes_from_the_first_node(home: Path) -> None:
    run_dir = _seed_run(home)
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "not-started"
    assert hint["retryable"] is True and hint["strategy"] == "resume"
    assert hint["from_node"] == "planner"
    assert hint["command"].endswith("--from-node planner")


def test_reaped_before_planning_surfaces_the_reaper_note(home: Path) -> None:
    """No plan either: the reaper's note IS the whole story."""
    _seed_run(home, notes="reaped: dispatcher pid 75073 gone (.pid written 2026-10-12T08:05:28)")
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "not-started"
    assert "reaped" in hint["needs_change"]["detail"]
    # Nothing to reuse, and no node to name.
    assert hint["from_node"] is None


def test_a_node_that_started_is_not_not_started(home: Path) -> None:
    """The case must assert an absence — one ``node_start`` retires it."""
    run_dir = _seed_run(home)
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    _insert_events(home, RUN, [("node_start", {"node_id": "planner"})])
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] != "not-started"


# ── structured verifier failures ───────────────────────────────────────────


def test_structured_verifier_missing_list_becomes_a_code_hint(home: Path) -> None:
    """A verifier that fails by listing absent keys is not "unclassified"."""
    run_dir = _seed_run(home)
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    _insert_events(home, RUN, [
        ("node_start", {"node_id": "planner"}),
        ("node_start", {"node_id": "static_check"}),
        ("node_end", {"node_id": "static_check", "finish_reason": "error"}),
    ])
    # ``read_verifier_payload`` matches on the ``verifier`` field, so it must
    # name this node's stem even though the *checker* is panel-completeness.
    _write_verifier(run_dir, "static-check", {
        "verifier": "static-check",
        "pass": False,
        "missing": ["laguna:finding_F2_missing:['corrections']"],
    })
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["needs_change"]["kind"] == "code"
    assert hint["failed_node"] == "static_check"
    assert "finding_F2_missing" in hint["needs_change"]["detail"]


def test_recipe_miss_falls_back_to_the_nodes_that_ran(home: Path) -> None:
    """An unresolvable recipe must not blind every artefact lookup.

    A per-run synthesised workflow (``orch-smoke``) or a deleted engine
    worktree leaves ``find_recipe`` empty; the node names in ``run_events``
    still identify the failure.
    """
    run_dir = _seed_run(home, recipe="ghost-recipe")
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    _insert_events(home, RUN, [
        ("node_start", {"node_id": "fail_probe"}),
        ("node_end", {"node_id": "fail_probe", "finish_reason": "error"}),
    ])
    _write_verifier(run_dir, "fail-probe", {
        "verifier": "fail-probe",
        "pass": False,
        "reasons": ["this verifier fails on purpose"],
    })
    hint = retry_hint.compute(home, RUN)
    assert hint is not None
    assert hint["failed_node"] == "fail_probe"
    assert hint["needs_change"]["kind"] == "code"
    assert "on purpose" in hint["needs_change"]["detail"]
