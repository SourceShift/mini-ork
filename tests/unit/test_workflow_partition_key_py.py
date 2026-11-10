"""`partition_key` is refused, not silently dropped.

The audit found the field declared in the schema and in
`recipes/bdd-first-delivery/workflow.yaml` while no Python read it: the
compiler ignored it, `dispatch_mode: partitioned` groups by node *type*, and
the bdd recipe does its per-sub-epic fan-out in its own `lib/dispatch.sh`.
A declared-but-unread field is worse than an absent one — the recipe claims a
data-driven fan-out it never gets.

The fix refuses the declaration with a message naming the sanctioned
mechanism (`mini_ork.orchestration.item_fanout`), and drops the property from
the schema so `mini-ork validate` says the same thing before a run starts.

Hermetic: the refusal is compiled from a tmp workflow, the recipe assertion
reads this checkout.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from mini_ork.workflow.compiler import WorkflowCompileError, compile_workflow

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "schemas" / "workflow.schema.json"
BDD_RECIPE = REPO / "recipes" / "bdd-first-delivery" / "workflow.yaml"


def _workflow(node_extras: dict, tmp_path: Path) -> Path:
    node = {
        "name": "implementer",
        "type": "implementer",
        "dispatch_mode": "partitioned",
        **node_extras,
    }
    doc = {
        "version": "0.1.0",
        "task_class": "tooling_change",
        "nodes": [node, {"name": "publisher", "type": "publisher", "dispatch_mode": "serial"}],
        "edges": [{"from": "implementer", "to": "publisher", "edge_type": "depends_on"}],
    }
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_the_compiler_refuses_a_declared_partition_key(tmp_path: Path) -> None:
    path = _workflow({"partition_key": "sub_epic_id"}, tmp_path)
    with pytest.raises(WorkflowCompileError) as excinfo:
        compile_workflow(path)
    message = str(excinfo.value)
    assert "partition_key" in message
    assert "item_fanout" in message, "the refusal must name the mechanism that does fan out"


def test_the_refusal_says_what_partitioned_actually_does(tmp_path: Path) -> None:
    path = _workflow({"partition_key": "lens_id"}, tmp_path)
    with pytest.raises(WorkflowCompileError) as excinfo:
        compile_workflow(path)
    message = str(excinfo.value)
    assert "groups by node type" in message
    assert "recipes/goal-loop" in message, "point at a working reference, not just a module name"


def test_a_partitioned_node_without_a_key_still_compiles(tmp_path: Path) -> None:
    """`dispatch_mode: partitioned` alone is legal; only the dead key is refused."""
    compiled = compile_workflow(_workflow({}, tmp_path))
    assert compiled.nodes["implementer"].dispatch_mode == "partitioned"


def test_the_schema_no_longer_offers_the_property() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    node_schema = schema["$defs"]["WorkflowNode"]
    assert "partition_key" not in node_schema["properties"]
    doc = {
        "version": "0.1.0",
        "task_class": "tooling_change",
        "nodes": [
            {
                "name": "implementer",
                "type": "implementer",
                "dispatch_mode": "partitioned",
                "partition_key": "sub_epic_id",
            }
        ],
        "edges": [],
    }
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = list(validator.iter_errors(doc))
    assert errors, "a recipe declaring partition_key must fail the schema, not pass silently"
    assert any("partition_key" in e.message for e in errors)


def test_the_bdd_recipe_declares_no_partition_key_and_still_compiles() -> None:
    document = yaml.safe_load(BDD_RECIPE.read_text(encoding="utf-8"))
    assert all("partition_key" not in node for node in document["nodes"])
    assert any(node["dispatch_mode"] == "partitioned" for node in document["nodes"]), (
        "the recipe still fans out by node type; only the dead key is gone"
    )
    compiled = compile_workflow(BDD_RECIPE)
    assert compiled.nodes["implementer"].dispatch_mode == "partitioned"
