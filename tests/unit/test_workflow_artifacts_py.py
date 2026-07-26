"""Artifact-port compiler, ledger, and transform integration tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import execute as ex
from mini_ork.workflow import ArtifactContractError, ArtifactLedger, WorkflowCompileError, compile_workflow
from mini_ork.workflow.transforms import execute_transform


@pytest.fixture(autouse=True)
def _isolate_run_environment(monkeypatch):
    for name in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_PLAN_PATH",
        "MINI_ORK_RUN_ID", "MO_TARGET_CWD", "MO_APPLY_IMPL_OUTPUT",
    ):
        monkeypatch.delenv(name, raising=False)


_WORKFLOW = """\
version: "0.2.0"
task_class: artifact_test
nodes:
  - name: synthesizer
    type: reviewer
    model_lane: reviewer
    dispatch_mode: serial
    inputs:
      panel_reports: { required: true }
    outputs: [{ name: synthesis, kind: markdown, path: synthesis.md }]
  - name: beta_lens
    type: researcher
    model_lane: beta_lens
    dispatch_mode: serial
    outputs: [{ name: report, kind: markdown, path: lens-beta.md }]
  - name: alpha_lens
    type: researcher
    model_lane: alpha_lens
    dispatch_mode: serial
    outputs: [{ name: report, kind: markdown, path: lens-alpha.md }]
  - name: anonymize_panel
    type: transform
    transform: panel.anonymize@v1
    dispatch_mode: serial
    inputs:
      reports: { required: true, many: true }
    outputs:
      - { name: panel_responses, kind: markdown, path: panel-responses.md }
      - { name: label_map, kind: json, path: workspace/system/anonymize-panel/label-map.json, visibility: system_only }
edges:
  - { from: alpha_lens, to: anonymize_panel, edge_type: supplies_context_to, from_output: report, to_input: reports }
  - { from: beta_lens, to: anonymize_panel, edge_type: supplies_context_to, from_output: report, to_input: reports }
  - { from: anonymize_panel, to: synthesizer, edge_type: supplies_context_to, from_output: panel_responses, to_input: panel_reports }
"""


def _workflow(tmp_path: Path) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(_WORKFLOW, encoding="utf-8")
    return path


def test_compiler_orders_artifact_consumers_after_producers(tmp_path):
    compiled = compile_workflow(_workflow(tmp_path))

    # Synthesizer is declared first to prove readiness comes from edges, not YAML position.
    assert compiled.topological_order == (
        "beta_lens", "alpha_lens", "anonymize_panel", "synthesizer"
    )
    assert [field.split("\x1f", 1)[0] for field in compiled.node_fields("\x1f")] == list(
        compiled.topological_order
    )


def test_compiler_rejects_system_only_artifact_for_agent(tmp_path):
    bad = _WORKFLOW.replace(
        "from: anonymize_panel, to: synthesizer, edge_type: supplies_context_to, from_output: panel_responses, to_input: panel_reports",
        "from: anonymize_panel, to: synthesizer, edge_type: supplies_context_to, from_output: label_map, to_input: panel_reports",
    )
    path = tmp_path / "bad.yaml"
    path.write_text(bad, encoding="utf-8")

    with pytest.raises(WorkflowCompileError, match="system-only artifact"):
        compile_workflow(path)


def test_compiler_rejects_duplicate_and_reserved_output_paths(tmp_path):
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        """\
version: "0.2.0"
task_class: artifact_test
nodes:
  - { name: first, type: researcher, outputs: [{ name: report, path: reports/shared.md }] }
  - { name: second, type: researcher, outputs: [{ name: report, path: reports/shared.md }] }
edges: []
""",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowCompileError, match="declared by both"):
        compile_workflow(duplicate)

    reserved = tmp_path / "reserved.yaml"
    reserved.write_text(
        """\
version: "0.2.0"
task_class: artifact_test
nodes:
  - { name: writer, type: researcher, outputs: [{ name: report, path: workspace/manifests/forged.json }] }
edges: []
""",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowCompileError, match="reserved ledger path"):
        compile_workflow(reserved)

    unsupported = tmp_path / "unsupported.yaml"
    unsupported.write_text(
        """\
version: "0.2.0"
task_class: artifact_test
nodes:
  - { name: planner, type: planner, outputs: [{ name: plan, path: plan.md }] }
edges: []
""",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowCompileError, match="does not publish artifacts"):
        compile_workflow(unsupported)


def test_ledger_materializes_anonymous_bundle_and_preserves_system_receipt(tmp_path):
    compiled = compile_workflow(_workflow(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ledger = ArtifactLedger(run_dir, "run-artifacts")

    (run_dir / "lens-alpha.md").write_text(
        "alpha_lens found src/a.py:12\n", encoding="utf-8"
    )
    (run_dir / "lens-beta.md").write_text(
        "beta lens found src/b.py:22\n", encoding="utf-8"
    )
    ledger.publish_node_outputs(compiled, "alpha_lens")
    ledger.publish_node_outputs(compiled, "beta_lens")

    ledger.prepare_inputs(compiled, "anonymize_panel")
    bundle = execute_transform(compiled, ledger, "anonymize_panel")
    ledger.publish_node_outputs(compiled, "anonymize_panel")
    prepared_synth = ledger.prepare_inputs(compiled, "synthesizer")

    bundle_text = bundle.read_text(encoding="utf-8").lower()
    assert "response a" in bundle_text and "response b" in bundle_text
    assert "alpha" not in bundle_text and "beta" not in bundle_text
    assert "redacted source" in bundle_text
    assert json.loads((run_dir / "workspace/system/anonymize-panel/label-map.json").read_text())
    assert len(prepared_synth.paths["panel_reports"]) == 1
    manifest = prepared_synth.manifest_path.read_text(encoding="utf-8")
    assert "lens-alpha" not in manifest and "lens-beta" not in manifest
    assert "label-map" not in manifest


def test_ledger_rejects_tampered_producer_artifact(tmp_path):
    compiled = compile_workflow(_workflow(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ledger = ArtifactLedger(run_dir, "run-integrity")
    (run_dir / "lens-alpha.md").write_text("first\n", encoding="utf-8")
    (run_dir / "lens-beta.md").write_text("second\n", encoding="utf-8")
    ledger.publish_node_outputs(compiled, "alpha_lens")
    ledger.publish_node_outputs(compiled, "beta_lens")
    (run_dir / "lens-alpha.md").write_text("changed after publication\n", encoding="utf-8")

    with pytest.raises(ArtifactContractError, match="integrity check failed"):
        ledger.prepare_inputs(compiled, "anonymize_panel")


def test_executor_wires_artifacts_through_transform_without_raw_lens_prompt(tmp_path):
    workflow = _workflow(tmp_path)
    compiled = compile_workflow(workflow)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    captured_prompts: list[str] = []

    def fake_dispatch(_task_class, lane, prompt):
        captured_prompts.append(prompt)
        if "Synthesize for:" in prompt:
            return 0, "# Synthesis\nResponse A and Response B agree.\n"
        return 0, f"{lane} found src/example.py:42\n"

    for node_id in compiled.topological_order:
        fields = compiled.nodes[node_id].dispatch_fields("\x1f").split("\x1f")
        rc, finish_reason = ex.dispatch_node(
            fields,
            root=str(REPO),
            run_dir=str(run_dir),
            plan_path="",
            task_class="artifact_test",
            db="",
            run_id="run-executor",
            dispatch_fn=fake_dispatch,
            workflow=str(workflow),
        )
        assert (rc, finish_reason) == (0, "done")

    synthesis_prompt = captured_prompts[-1].lower()
    assert "declared artifact inputs" in synthesis_prompt
    assert "panel-responses.md" in synthesis_prompt
    assert "lens-alpha" not in synthesis_prompt and "lens-beta" not in synthesis_prompt
    assert "alpha_lens" not in synthesis_prompt and "beta_lens" not in synthesis_prompt
    assert "label-map" not in synthesis_prompt
    assert (run_dir / "synthesis.md").is_file()


def test_executor_uses_declared_single_output_path_for_new_recipe(tmp_path):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
version: "0.2.0"
task_class: artifact_test
nodes:
  - name: evidence
    type: researcher
    model_lane: researcher
    dispatch_mode: serial
    outputs: [{ name: report, kind: markdown, path: reports/custom-report.md }]
edges: []
""",
        encoding="utf-8",
    )
    compiled = compile_workflow(workflow)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc, finish_reason = ex.dispatch_node(
        compiled.nodes["evidence"].dispatch_fields("\x1f").split("\x1f"),
        root=str(REPO),
        run_dir=str(run_dir),
        plan_path="",
        task_class="artifact_test",
        db="",
        run_id="run-declared-path",
        dispatch_fn=lambda *_args: (0, "finding src/example.py:42\n"),
        workflow=str(workflow),
    )

    assert (rc, finish_reason) == (0, "done")
    assert (run_dir / "reports/custom-report.md").is_file()
    assert not (run_dir / "context-evidence.json").exists()


def test_implementer_uses_declared_single_output_path(tmp_path, monkeypatch):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
version: "0.2.0"
task_class: artifact_test
nodes:
  - name: implement
    type: implementer
    model_lane: implementer
    dispatch_mode: serial
    outputs: [{ name: summary, kind: markdown, path: reports/implementation.md }]
edges: []
""",
        encoding="utf-8",
    )
    compiled = compile_workflow(workflow)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_APPLY_IMPL_OUTPUT", "0")
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))

    rc, finish_reason = ex.dispatch_node(
        compiled.nodes["implement"].dispatch_fields("\x1f").split("\x1f"),
        root=str(REPO),
        run_dir=str(run_dir),
        plan_path="",
        task_class="artifact_test",
        db="",
        run_id="run-implementer-output",
        dispatch_fn=lambda *_args: (0, "implemented src/example.py\n"),
        workflow=str(workflow),
    )

    assert (rc, finish_reason) == (0, "done")
    assert (run_dir / "reports/implementation.md").is_file()
    assert not (run_dir / "impl-implement.log").exists()


def test_consumer_integrity_failure_is_an_artifact_contract_error(tmp_path):
    workflow = _workflow(tmp_path)
    compiled = compile_workflow(workflow)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ledger = ArtifactLedger(run_dir, "run-integrity-finish-reason")
    (run_dir / "lens-alpha.md").write_text("original\n", encoding="utf-8")
    ledger.publish_node_outputs(compiled, "alpha_lens")
    (run_dir / "lens-alpha.md").write_text("tampered\n", encoding="utf-8")

    rc, finish_reason = ex.dispatch_node(
        compiled.nodes["anonymize_panel"].dispatch_fields("\x1f").split("\x1f"),
        root=str(REPO),
        run_dir=str(run_dir),
        plan_path="",
        task_class="artifact_test",
        db="",
        run_id="run-integrity-finish-reason",
        dispatch_fn=lambda *_args: (0, "unreachable"),
        workflow=str(workflow),
    )

    assert (rc, finish_reason) == (1, "artifact_contract")


def test_publisher_publishes_declared_outputs(tmp_path, monkeypatch):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        """\
version: "0.2.0"
task_class: artifact_test
nodes:
  - name: publish
    type: publisher
    model_lane: publisher
    dispatch_mode: serial
    outputs: [{ name: delivery, kind: markdown, path: delivered.md }]
edges: []
""",
        encoding="utf-8",
    )
    compiled = compile_workflow(workflow)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "delivered.md").write_text("delivered\n", encoding="utf-8")
    monkeypatch.setattr(ex, "publisher_node", lambda *_args, **_kwargs: (0, "done"))

    rc, finish_reason = ex.dispatch_node(
        compiled.nodes["publish"].dispatch_fields("\x1f").split("\x1f"),
        root=str(REPO),
        run_dir=str(run_dir),
        plan_path="",
        task_class="artifact_test",
        db="",
        run_id="run-publisher-output",
        dispatch_fn=lambda *_args: (0, "unused"),
        workflow=str(workflow),
    )

    assert (rc, finish_reason) == (0, "done")
    assert (run_dir / "workspace/manifests/publish.outputs.json").is_file()


def test_refactor_audit_verifier_requires_anonymous_panel_bundle(tmp_path):
    for lens in ("glm", "kimi", "codex", "opus", "minimax"):
        (tmp_path / f"lens-{lens}.md").write_text(
            "\n".join(f"finding src/{lens}.py:{line}" for line in range(1, 12)) + "\n",
            encoding="utf-8",
        )
    (tmp_path / "synthesis.md").write_text(
        "\n".join(f"Response {label} supports {label}-1" for label in "ABCDE") + "\n",
        encoding="utf-8",
    )
    script = REPO / "recipes/refactor-audit/verifiers/lens-completeness.sh"
    env = {**os.environ, "MINI_ORK_RUN_DIR": str(tmp_path)}

    missing_panel = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    assert missing_panel.returncode == 0
    assert json.loads(missing_panel.stdout)["pass"] is False

    (tmp_path / "panel-responses.md").write_text(
        "\n".join(f"## Response {label}" for label in "ABCDE") + "\n",
        encoding="utf-8",
    )
    complete = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    assert complete.returncode == 0
    assert json.loads(complete.stdout)["pass"] is True
