"""Contracts for the artifact-led DSPy prompt graph recipe."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from mini_ork.workflow import compile_workflow


REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "recipes" / "prompt-graph-loop"


def _edge(document, source, target):
    return next(edge for edge in document["edges"] if edge["from"] == source and edge["to"] == target)


def test_prompt_graph_loop_compiles_with_explicit_artifact_handoffs():
    compiled = compile_workflow(RECIPE / "workflow.yaml")

    assert compiled.topological_order[:6] == (
        "prompt_intake",
        "semantic_flow_extractor",
        "source_researcher",
        "recursive_plan_composer",
        "draft_executor",
        "verifier_gate",
    )
    assert {binding.consumer_input for binding in compiled.bindings_for("dspy_exporter")} == {
        "agent_plan",
        "verification_report",
        "human_decision",
    }
    assert compiled.nodes["semantic_flow_extractor"].inputs["human_feedback"].required is False
    assert compiled.nodes["recursive_plan_composer"].inputs["refinement_prompt"].required is False
    assert "source_corpus" in compiled.nodes["recursive_plan_composer"].inputs


def test_recipe_preserves_dspy_feedback_and_approval_routes():
    import yaml

    document = yaml.safe_load((RECIPE / "workflow.yaml").read_text(encoding="utf-8"))
    reflection = _edge(document, "reflection_loop", "recursive_plan_composer")
    feedback = _edge(document, "human_feedback_gate", "semantic_flow_extractor")
    export = _edge(document, "human_feedback_gate", "dspy_exporter")

    assert reflection["recursive"] is True
    assert reflection["from_output"] == "refinement_prompt"
    assert feedback["recursive"] is True
    assert feedback["condition"] == "revise"
    assert feedback["from_output"] == "human_decision"
    assert export["edge_type"] == "human_decision_gate"
    assert export["condition"] == "approved"
    assert document["recursion"]["max_iterations"] == 5
    assert document["human_decision_artifact"] == "human-review-packet.md"
    assert document["selected_option_artifact"] == "human-decision.json"


def test_graph_contract_verifier_writes_a_receipt(tmp_path):
    (tmp_path / "agent-graph.json").write_text(
        json.dumps({"nodes": [{"id": "intake"}], "edges": [], "artifacts": []}),
        encoding="utf-8",
    )
    (tmp_path / "draft-artifact.md").write_text("## Artifact Contract\n", encoding="utf-8")
    (tmp_path / "verification-report.json").write_text(
        json.dumps(
            {
                "verdict": "pass",
                "claims_checked": [],
                "graph_completeness": "complete",
                "output_contract": "fit",
                "findings": [],
                "next_action": "request approval",
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(RECIPE / "verifiers" / "graph-contract.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "MINI_ORK_RUN_DIR": str(tmp_path)},
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "graph-contract-report.json").read_text(encoding="utf-8")) == {
        "pass": True,
        "errors": [],
    }


def test_human_feedback_gate_blocks_export_when_a_human_requests_revision(tmp_path):
    decision = tmp_path / "human-decision.json"
    decision.write_text(
        json.dumps({"decision": "revise", "approver": "operator", "feedback_delta": "Add sources."}),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(RECIPE / "verifiers" / "human-decision.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "MINI_ORK_RUN_DIR": str(tmp_path)},
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "pass": False,
        "status": "revision_requested",
        "errors": [],
    }


def test_human_feedback_gate_accepts_an_approved_durable_decision(tmp_path):
    (tmp_path / "human-decision.json").write_text(
        json.dumps({"decision": "approved", "approver": "operator"}),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(RECIPE / "verifiers" / "human-decision.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "MINI_ORK_RUN_DIR": str(tmp_path)},
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"pass": True, "status": "approved", "errors": []}
