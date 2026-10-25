"""ECO6 K13 — per-node ``gates:`` and node ``max_cost_usd`` are gone.

The compiler never read ``gates`` and the executor/handlers/publisher never
read it either; wiring it up would fail every node, because
``gate_registry.gate_evaluate`` resolves a gate by registered *id*, not by the
declared type name. The real gates run per task class from ``mini-ork verify``.
This suite is hermetic: every recipe is loaded/compiled/schema-checked, and the
negative cases build their fixtures under ``tmp_path``.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

import mini_ork

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "workflow.schema.json"
CODEMOD = REPO_ROOT / "scripts" / "codemods" / "eco6_strip_node_gates.py"
RECIPE_WORKFLOWS = sorted((REPO_ROOT / "recipes").glob("*/workflow.yaml"))


# ── shipped recipes: no node gates, schema-valid, compilable ────────────────


def test_recipe_corpus_is_present():
    assert RECIPE_WORKFLOWS, "no recipes/*/workflow.yaml found under the repo root"


@pytest.mark.parametrize("wf_path", RECIPE_WORKFLOWS,
                         ids=[p.parent.name for p in RECIPE_WORKFLOWS])
def test_recipe_node_declares_no_gates(wf_path):
    doc = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
    offenders = [
        node.get("name", f"<unnamed #{i}>")
        for i, node in enumerate(doc.get("nodes") or [])
        if isinstance(node, dict) and "gates" in node
    ]
    assert not offenders, (
        f"{wf_path.relative_to(REPO_ROOT)}: nodes still declare gates: {offenders}"
    )


@pytest.mark.parametrize("wf_path", RECIPE_WORKFLOWS,
                         ids=[p.parent.name for p in RECIPE_WORKFLOWS])
def test_recipe_validates_against_workflow_schema(wf_path):
    from jsonschema.validators import validator_for

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    document = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
    errors = [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator_for(schema)(schema).iter_errors(document)
    ]
    assert not errors, f"{wf_path.relative_to(REPO_ROOT)}: {errors}"


@pytest.mark.parametrize("wf_path", RECIPE_WORKFLOWS,
                         ids=[p.parent.name for p in RECIPE_WORKFLOWS])
def test_recipe_still_compiles(wf_path):
    from mini_ork.workflow import compile_workflow

    assert compile_workflow(str(wf_path)) is not None


def test_schema_node_definition_has_neither_key():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    props = schema["$defs"]["WorkflowNode"].get("properties", {})
    assert "gates" not in props
    assert "max_cost_usd" not in props
    assert "max_cost_usd" not in SCHEMA_PATH.read_text(encoding="utf-8")


# ── codemod: byte-preserving, node-only ────────────────────────────────────

SOURCE = '''version: "0.1.0"
task_class: codemod_probe
description: "byte-preservation probe"

# a top-level gates key is not a node gate and must survive byte-for-byte
gates:
  - task_class_gate
  - budget_gate

nodes:
  # comment above the first node
  - name: planner
    type: planner
    model_lane: planner
    prompt_ref: prompts/planner.md
    dispatch_mode: serial

  # comment between nodes
  - { name: researcher, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md, dispatch_mode: parallel, gates: [budget_gate] }

  - name: reviewer
    type: reviewer
    model_lane: reviewer
    prompt_ref: prompts/reviewer.md
    dispatch_mode: serial
    gates:
      - scope_gate
      - budget_gate
    tools:
      native: [Read]
      mcp: [codegraph]

edges:
  - { from: planner, to: researcher }
  - { from: researcher, to: reviewer }
'''

EXPECTED = '''version: "0.1.0"
task_class: codemod_probe
description: "byte-preservation probe"

# a top-level gates key is not a node gate and must survive byte-for-byte
gates:
  - task_class_gate
  - budget_gate

nodes:
  # comment above the first node
  - name: planner
    type: planner
    model_lane: planner
    prompt_ref: prompts/planner.md
    dispatch_mode: serial

  # comment between nodes
  - { name: researcher, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md, dispatch_mode: parallel }

  - name: reviewer
    type: reviewer
    model_lane: reviewer
    prompt_ref: prompts/reviewer.md
    dispatch_mode: serial
    tools:
      native: [Read]
      mcp: [codegraph]

edges:
  - { from: planner, to: researcher }
  - { from: researcher, to: reviewer }
'''


def _load_codemod():
    spec = importlib.util.spec_from_file_location("eco6_strip_node_gates_k13", CODEMOD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codemod_is_checked_in_and_byte_preserving():
    assert CODEMOD.is_file()
    assert _load_codemod().strip_node_gates(SOURCE) == EXPECTED


def test_codemod_removes_only_node_gate_dicts(tmp_path):
    target = tmp_path / "workflow.yaml"
    target.write_text(SOURCE, encoding="utf-8")
    assert _load_codemod().main([str(target)]) == 0

    after = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    before = yaml.safe_load(SOURCE) or {}
    assert after["nodes"] == [
        {k: v for k, v in node.items() if k != "gates"} for node in before["nodes"]
    ]
    # Top-level key and comments survive byte-for-byte.
    assert after["gates"] == ["task_class_gate", "budget_gate"]
    assert "# a top-level gates key is not a node gate" in target.read_text(encoding="utf-8")
    assert "# comment between nodes" in target.read_text(encoding="utf-8")


# ── validate: a stale recipe is a clear error ─────────────────────────────

_STALE_WORKFLOW = """version: "0.1.0"
task_class: stub_probe
description: stale recipe that still declares per-node gates

nodes:
  - name: researcher
    type: researcher
    model_lane: lens
    prompt_ref: prompts/lens.md
    dispatch_mode: serial
    gates: [budget_gate]

edges: []
"""


def test_validate_flags_stale_recipe(monkeypatch, tmp_path, capfd):
    from mini_ork.cli import validate

    root = tmp_path / "root"
    recipe_dir = root / "recipes" / "stale-gates"
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "task_class.yaml").write_text(
        "name: stub_probe\ndescription: hermetic fixture\n", encoding="utf-8")
    (recipe_dir / "workflow.yaml").write_text(_STALE_WORKFLOW, encoding="utf-8")
    (recipe_dir / "artifact_contract.yaml").write_text("outputs: []\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)

    assert validate.main(["--recipe", "stale-gates"]) == 1
    err = capfd.readouterr().err
    assert (
        "recipe stale-gates node 'researcher' declares gates: "
        "— per-node gates were never enforced"
    ) in err
    assert ("remove it; add a verifier node, or register a task-class gate with "
            "mini_ork.gates.gate_registry.gate_register()") in err


# ── types ─────────────────────────────────────────────────────────────────


def test_nodespec_has_no_gates_attribute():
    from mini_ork.types import NodeSpec

    assert not hasattr(NodeSpec, "gates")
    assert "gates" not in NodeSpec(name="planner", type="planner").to_dict()
