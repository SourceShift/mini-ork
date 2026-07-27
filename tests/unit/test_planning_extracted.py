"""Unit tests for the pure planner helpers extracted to ``mini_ork.planning``.

Covers plan-JSON extraction/validation (``plan_schema``) and the deterministic
recipe fallback + artifact-contract overlay (``recipe_plan``), plus the
re-export surface on ``mini_ork.cli.plan``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import plan as cli_plan
from mini_ork.planning import plan_schema, recipe_plan

_VALID = {
    "objective": "Ship widget", "assumptions": ["a"],
    "decomposition": [{"id": "s1", "description": "do", "node_type": "implementer", "depends_on": []}],
    "dependencies": [], "risk_notes": [],
    "artifact_contract": {"outputs": ["x"], "success_verifiers": ["v"]},
    "verifier_contract": {"checks": [{"id": "c1", "description": "check it"}]},
}


# ── re-export surface ──

def test_cli_plan_reexports_moved_names():
    for name in ("extract_plan_json", "validate_plan", "recipe_fallback_plan",
                 "overlay_plan", "_detect_truncation", "_contains_placeholder",
                 "_is_stub_string", "_is_plan", "_objects", "_NODE_TYPES",
                 "_PLACEHOLDER_HINT"):
        assert hasattr(cli_plan, name), name
    assert cli_plan.extract_plan_json is plan_schema.extract_plan_json
    assert cli_plan.validate_plan is plan_schema.validate_plan
    assert cli_plan.recipe_fallback_plan is recipe_plan.recipe_fallback_plan
    assert cli_plan.overlay_plan is recipe_plan.overlay_plan


# ── plan_schema._objects ──

def test_objects_yields_balanced_json_chunks():
    chunks = list(plan_schema._objects('pre {"a": {"b": 1}} mid {"c": "}"} post'))
    assert chunks == ['{"a": {"b": 1}}', '{"c": "}"}']


def test_objects_skips_unbalanced_tail():
    assert list(plan_schema._objects('{"a": 1} {"unterminated')) == ['{"a": 1}']
    assert list(plan_schema._objects("no braces here")) == []


# ── plan_schema._is_stub_string / _contains_placeholder ──

def test_is_stub_string():
    assert plan_schema._is_stub_string("<TODO>")
    assert plan_schema._is_stub_string("<dry-run: not generated>")
    assert plan_schema._is_stub_string("<>")
    assert not plan_schema._is_stub_string("<ContentNodeCreationModal>")
    assert not plan_schema._is_stub_string("<div>")
    assert not plan_schema._is_stub_string("plain string")
    assert not plan_schema._is_stub_string(42)


def test_contains_placeholder_scoped_to_plan_shell():
    assert plan_schema._contains_placeholder({"objective": "<TODO>"})
    assert plan_schema._contains_placeholder({"objective": "", "decomposition": []})
    assert not plan_schema._contains_placeholder({
        "objective": "real", "decomposition": [{"description": "<shell-only>"}]})
    assert plan_schema._contains_placeholder("<fill me>")
    assert not plan_schema._contains_placeholder("real")


# ── plan_schema.extract_plan_json ──

def test_extract_picks_first_valid_plan_over_garbage():
    raw = '{"junk": 1}\n```json\n' + json.dumps(_VALID) + "\n```\n"
    out = plan_schema.extract_plan_json(raw)
    assert json.loads(out)["objective"] == "Ship widget"


def test_extract_falls_back_to_first_object_or_raw():
    assert plan_schema.extract_plan_json('{"a": 1}') == '{"a": 1}'
    assert plan_schema.extract_plan_json("not json") == "not json"


# ── plan_schema.validate_plan verdicts ──

def test_validate_plan_verdict_matrix():
    assert plan_schema.validate_plan(json.dumps(_VALID)) == "ok"
    assert plan_schema.validate_plan("not json") == "parse_error"

    no_vc = {**_VALID, "verifier_contract": {"checks": []}}
    assert plan_schema.validate_plan(json.dumps(no_vc)) == "missing_verifier_contract"

    bad_ac = {**_VALID, "artifact_contract": ["not-a-dict"]}
    assert plan_schema.validate_plan(json.dumps(bad_ac)) == "bad_artifact_contract"

    empty_nt = {**_VALID, "decomposition": [{"id": "s1", "node_type": "", "depends_on": []}]}
    assert plan_schema.validate_plan(json.dumps(empty_nt)) == "bad_node_types"

    bad_nt = {**_VALID, "decomposition": [{"id": "s1", "node_type": "wizard", "depends_on": []}]}
    assert plan_schema.validate_plan(json.dumps(bad_nt)) == "bad_node_types"


# ── plan_schema._detect_truncation ──

def test_detect_truncation():
    assert plan_schema._detect_truncation('{"objective": "x", "decomposition": [{"id":')
    assert not plan_schema._detect_truncation(json.dumps(_VALID))
    assert not plan_schema._detect_truncation("")
    assert not plan_schema._detect_truncation(None)


# ── recipe_plan.recipe_fallback_plan ──

def test_recipe_fallback_plan_none_without_inputs(tmp_path):
    assert recipe_plan.recipe_fallback_plan("", "", str(tmp_path), "k.md") is None
    assert recipe_plan.recipe_fallback_plan("demo", str(tmp_path / "missing.yaml"),
                                            str(tmp_path), "k.md") is None


def test_recipe_fallback_plan_builds_from_workflow(tmp_path):
    recipe = tmp_path / "recipes" / "demo"
    recipe.mkdir(parents=True)
    workflow = recipe / "workflow.yaml"
    workflow.write_text(
        "nodes:\n"
        "  - name: implement\n"
        "    type: implementer\n"
        "  - name: verify\n"
        "    type: verifier\n"
        "edges:\n"
        "  - from: implement\n"
        "    to: verify\n"
        "outputs:\n"
        "  - plan.json\n"
        "success_verifiers:\n"
        "  - verifiers/test.py\n"
    )
    out = recipe_plan.recipe_fallback_plan("demo", str(workflow), str(tmp_path), "k.md")
    p = json.loads(out)
    assert p["objective"].startswith("Execute recipe demo for k.md")
    assert [s["id"] for s in p["decomposition"]] == ["implement", "verify"]
    assert p["decomposition"][1]["depends_on"] == ["implement"]
    assert p["dependencies"] == [{"from": "implement", "to": "verify"}]
    assert p["artifact_contract"] == {"outputs": ["plan.json"],
                                      "success_verifiers": ["verifiers/test.py"]}
    assert p["verifier_contract"]["checks"]  # non-empty → validate_plan ok
    assert plan_schema.validate_plan(out) == "ok"


# ── recipe_plan.overlay_plan ──

def test_overlay_plan_passthrough_on_bad_json():
    assert recipe_plan.overlay_plan("not json", "code_fix", "", "/root") == "not json"


def test_overlay_plan_stamps_task_class_without_contract(tmp_path):
    out = json.loads(recipe_plan.overlay_plan(json.dumps(_VALID), "code_fix", "",
                                              str(tmp_path)))
    assert out["task_class"] == "code_fix"
    assert out["artifact_contract"] == _VALID["artifact_contract"]


def test_overlay_plan_applies_recipe_contract(tmp_path):
    recipe = tmp_path / "recipes" / "demo"
    recipe.mkdir(parents=True)
    (recipe / "artifact_contract.yaml").write_text(
        "outputs:\n  - out.md\nsuccess_verifiers:\n  - verifiers/check.py\n"
    )
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"recipe": "demo"}))
    out = json.loads(recipe_plan.overlay_plan(json.dumps(_VALID), "code_fix",
                                              str(profile), str(tmp_path)))
    ac = out["artifact_contract"]
    assert ac["success_verifiers"] == ["verifiers/check.py"]
    assert ac["outputs"] == ["x"]  # setdefault keeps the planner's outputs
    # planner prose verifiers are preserved as acceptance_criteria
    assert ac["acceptance_criteria"] == ["v"]
