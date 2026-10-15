"""``retries`` edges (the revise loop) are control flow, not dependencies.

framework-edit gained ``{from: reviewer, to: implementer, edge_type: retries}``
back-edges in 2be82523; counting them made the recovery DAG cyclic and
``mini-ork recover`` refused every framework-edit run (2026-10-07).
"""
from __future__ import annotations

from pathlib import Path

from mini_ork.recovery.dag import load_dag

REPO = Path(__file__).resolve().parents[2]


def _write(tmp_path: Path, edges: str) -> str:
    p = tmp_path / "workflow.yaml"
    p.write_text(
        "nodes:\n"
        "  - {name: lens, type: researcher}\n"
        "  - {name: implementer, type: implementer}\n"
        "  - {name: reviewer, type: reviewer}\n"
        "  - {name: rollback, type: rollback}\n"
        "edges:\n" + edges, encoding="utf-8")
    return str(p)


def test_retries_and_escalates_to_are_not_dependencies(tmp_path: Path) -> None:
    dag = load_dag(_write(tmp_path,
        "  - {from: lens, to: implementer, edge_type: supplies_context_to}\n"
        "  - {from: implementer, to: reviewer, edge_type: verifies}\n"
        "  - {from: reviewer, to: implementer, edge_type: retries, max_rounds: 2}\n"
        "  - {from: reviewer, to: rollback, edge_type: escalates_to}\n"))
    assert tuple(dag.parents["implementer"]) == ("lens",)
    assert "reviewer" not in dag.parents["implementer"]
    assert tuple(dag.parents["rollback"]) == ()


def test_real_framework_edit_workflow_has_no_cycle() -> None:
    dag = load_dag(str(REPO / "recipes" / "framework-edit" / "workflow.yaml"))
    assert "reviewer" not in dag.parents["implementer"]
    # spec_test_author is a real dependency: the blind spec tests are authored
    # (from the kickoff alone) before the implementer starts, so they are an
    # acceptance bar, not a retry/ephemeral edge. Verifiers and rollback still
    # must not be parents.
    assert set(dag.parents["implementer"]) == {
        "code_impact_lens", "prior_art_lens", "spec_test_author"}
