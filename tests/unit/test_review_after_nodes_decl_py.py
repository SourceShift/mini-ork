"""Workflow-declared operator-review pauses (``review_after_nodes``).

The workflow key merges into the review policy's ``after_nodes`` so the
existing node-review machinery parks the run after a node the recipe names.
These tests are hermetic: every ``MINI_ORK_HOME`` points at an empty temp dir,
so no real ``config/review.yaml`` leaks into the resolver.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import ValidationError, validate

from mini_ork.cli.execute import _declared_review_nodes, main

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "workflow.schema.json"
RECIPE_PATH = REPO_ROOT / "recipes" / "spec-driven-delivery" / "workflow.yaml"


_MINIMAL = {
    "version": "0.1.0",
    "task_class": "framework_edit",
    "nodes": [
        {"name": "contract_compiler", "type": "verifier",
         "verifier_ref": "verifiers/contract.py", "dispatch_mode": "serial"},
        {"name": "sink", "type": "verifier",
         "verifier_ref": "verifiers/sink.py", "dispatch_mode": "serial"},
    ],
    "edges": [
        {"from": "contract_compiler", "to": "sink", "edge_type": "depends_on"},
    ],
}


@pytest.fixture
def empty_home(tmp_path, monkeypatch):
    """An empty ``$MINI_ORK_HOME`` so no ``config/review.yaml`` is read."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MO_REVIEW_AFTER_NODES", raising=False)
    monkeypatch.delenv("MO_REVIEW_RECIPE_NODES", raising=False)
    return home


def _workflow(tmp_path, **extra) -> str:
    doc = copy.deepcopy(_MINIMAL)
    doc.update(extra)
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(path)


# ── _declared_review_nodes ─────────────────────────────────────────────


def test_no_key_gives_empty(tmp_path, empty_home):
    nodes, message = _declared_review_nodes(
        _workflow(tmp_path), {"contract_compiler", "sink"}, {})
    assert nodes == ()
    assert message == ""


def test_declared_node_is_returned(tmp_path, empty_home):
    wf = _workflow(tmp_path, review_after_nodes=["contract_compiler"])
    nodes, _ = _declared_review_nodes(wf, {"contract_compiler", "x"}, {})
    assert nodes == ("contract_compiler",)


def test_policy_after_nodes_precede_declared(tmp_path, empty_home):
    wf = _workflow(tmp_path, review_after_nodes=["contract_compiler"])
    nodes, _ = _declared_review_nodes(
        wf, {"contract_compiler", "sink"}, {"MO_REVIEW_AFTER_NODES": "reviewer"})
    assert nodes == ("reviewer", "contract_compiler")


def test_merge_deduplicates_and_preserves_order(tmp_path, empty_home):
    wf = _workflow(tmp_path, review_after_nodes=["contract_compiler", "reviewer"])
    nodes, _ = _declared_review_nodes(
        wf, {"contract_compiler", "sink", "reviewer"},
        {"MO_REVIEW_AFTER_NODES": "contract_compiler,gate"})
    assert nodes == ("contract_compiler", "gate", "reviewer")


def test_knob_zero_disables_and_message_names_it(tmp_path, empty_home):
    wf = _workflow(tmp_path, review_after_nodes=["contract_compiler"])
    nodes, message = _declared_review_nodes(
        wf, {"contract_compiler", "sink"}, {"MO_REVIEW_RECIPE_NODES": "0"})
    assert nodes == ()
    assert "MO_REVIEW_RECIPE_NODES" in message


def test_knob_zero_wins_even_with_env_after_nodes(tmp_path, empty_home):
    wf = _workflow(tmp_path, review_after_nodes=["contract_compiler"])
    nodes, message = _declared_review_nodes(
        wf, {"contract_compiler", "sink"},
        {"MO_REVIEW_RECIPE_NODES": "0", "MO_REVIEW_AFTER_NODES": "reviewer"})
    assert nodes == ()
    assert "MO_REVIEW_RECIPE_NODES" in message


def test_unknown_node_raises_naming_it(tmp_path, empty_home):
    wf = _workflow(tmp_path, review_after_nodes=["nope"])
    with pytest.raises(ValueError) as excinfo:
        _declared_review_nodes(wf, {"contract_compiler", "sink"}, {})
    assert "nope" in str(excinfo.value)


@pytest.mark.parametrize("node_type", ["verifier", "publisher", "rollback", "eval", "reflector"])
def test_non_checkpointing_node_type_raises(tmp_path, empty_home, node_type):
    # These handlers publish no node_checkpoints row, so a pause after one
    # would never park the run; the declaration must fail loudly instead.
    wf = _workflow(tmp_path, review_after_nodes=["gate"])
    with pytest.raises(ValueError) as excinfo:
        _declared_review_nodes(wf, {"gate": node_type, "sink": "publisher"}, {})
    assert "'gate'" in str(excinfo.value) and node_type in str(excinfo.value)
    assert "never" in str(excinfo.value)


@pytest.mark.parametrize("node_type", ["researcher", "implementer", "reviewer", "transform", "planner"])
def test_checkpointing_node_type_is_accepted(tmp_path, empty_home, node_type):
    wf = _workflow(tmp_path, review_after_nodes=["gate"])
    nodes, _ = _declared_review_nodes(wf, {"gate": node_type}, {})
    assert nodes == ("gate",)


def test_spec_driven_delivery_declares_a_checkpointing_node():
    import mini_ork
    from mini_ork.workflow import compile_workflow

    wf = Path(mini_ork.__file__).resolve().parents[1] / "recipes" / "spec-driven-delivery" / "workflow.yaml"
    compiled = compile_workflow(str(wf))
    types = {name: node.type for name, node in compiled.nodes.items()}
    nodes, _ = _declared_review_nodes(str(wf), types, {"MO_REVIEW_RECIPE_NODES": "1"})
    assert "contract_compiler" in nodes


def test_non_list_value_raises(tmp_path, empty_home):
    wf = _workflow(tmp_path, review_after_nodes="contract_compiler")
    with pytest.raises(ValueError):
        _declared_review_nodes(wf, {"contract_compiler", "sink"}, {})


def test_non_string_item_raises(tmp_path, empty_home):
    wf = _workflow(tmp_path, review_after_nodes=["contract_compiler", 2])
    with pytest.raises(ValueError):
        _declared_review_nodes(wf, {"contract_compiler", "sink"}, {})


# ── schemas/workflow.schema.json ──────────────────────────────────────


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def recipe_doc():
    return yaml.safe_load(RECIPE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def base_doc(recipe_doc):
    doc = copy.deepcopy(recipe_doc)
    doc.pop("review_after_nodes", None)
    return doc


def test_schema_accepts_review_after_nodes(schema, base_doc):
    base_doc["review_after_nodes"] = ["a"]
    validate(instance=base_doc, schema=schema)


@pytest.mark.parametrize("bad", ["a", ["a", "a"], [""]])
def test_schema_rejects_bad_shapes(schema, base_doc, bad):
    base_doc["review_after_nodes"] = bad
    with pytest.raises(ValidationError):
        validate(instance=base_doc, schema=schema)


def test_schema_still_rejects_unknown_top_level_keys(schema, base_doc):
    base_doc["not_a_real_property"] = ["x"]
    with pytest.raises(ValidationError):
        validate(instance=base_doc, schema=schema)


# ── recipes/spec-driven-delivery/workflow.yaml ────────────────────────


def test_recipe_declares_contract_compiler(recipe_doc):
    declared = recipe_doc.get("review_after_nodes")
    assert isinstance(declared, list)
    assert "contract_compiler" in declared


def test_recipe_validates_and_names_the_node(schema, recipe_doc):
    validate(instance=recipe_doc, schema=schema)
    names = [node["name"] for node in recipe_doc["nodes"]]
    assert "contract_compiler" in names
    assert "contract_compiler" in recipe_doc["review_after_nodes"]


# ── main wiring ───────────────────────────────────────────────────────


def test_main_unknown_declared_node_returns_2_without_dispatch(
        tmp_path, monkeypatch, capsys):
    wf = _workflow(tmp_path, review_after_nodes=["nope"])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = run_dir / "plan.json"
    plan.write_text(
        json.dumps({"plan_status": "ready", "decomposition": []}),
        encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    # Pin every key main may read or write: pre-setting via monkeypatch makes
    # teardown restore the prior process state even if the code overwrites them.
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO_ROOT))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "r-test")
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("MINI_ORK_WORKFLOW", wf)
    monkeypatch.delenv("MINI_ORK_PLAN_PATH", raising=False)
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)
    monkeypatch.delenv("MINI_ORK_RECOVERY_FROM", raising=False)
    monkeypatch.delenv("MINI_ORK_RECOVERY_CLOSURE", raising=False)
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)
    monkeypatch.delenv("MO_REVIEW_AFTER_NODES", raising=False)
    monkeypatch.delenv("MO_REVIEW_RECIPE_NODES", raising=False)

    calls = []

    def recorder(*args, **kwargs):
        calls.append((args, kwargs))
        return 0, "ok"

    rc = main([str(plan)], dispatch_fn=recorder)
    err = capsys.readouterr().err
    assert rc == 2
    assert "execute: workflow review_after_nodes:" in err
    assert "nope" in err
    assert calls == []
