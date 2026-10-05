"""Project (home-overlay) recipes contribute their artifact contract and their
verifiers, as engine recipes do. Without this every project recipe's run ended
with verdict "vacuous": the plan carried no success_verifiers and the verify
stage could not find the recipe's scripts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.cli import verify
from mini_ork.planning.recipe_plan import overlay_plan, recipe_dir, recipe_fallback_plan


def _project_recipe(home: Path, rid: str = "my-check") -> Path:
    d = home / "recipes" / rid
    (d / "verifiers").mkdir(parents=True)
    (d / "workflow.yaml").write_text(
        "version: '0.1.0'\ntask_class: my_check\nnodes:\n"
        "  - {name: edit, type: implementer, dispatch_mode: serial}\n"
        "  - {name: probe, type: verifier, dispatch_mode: serial, verifier_ref: verifiers/probe.py}\n"
        "edges:\n  - {from: edit, to: probe, edge_type: verifies}\n")
    (d / "artifact_contract.yaml").write_text(
        "task_class: my_check\nexpected_artifact: x\nsuccess_verifiers:\n  - verifiers/probe.py\n"
        "failure_policy: request_changes\n")
    (d / "verifiers" / "probe.py").write_text("print('{\"pass\": true}')\n")
    return d


@pytest.fixture
def engine_and_home(tmp_path, monkeypatch):
    engine, home = tmp_path / "engine", tmp_path / "proj" / ".mini-ork"
    (engine / "recipes").mkdir(parents=True)
    home.mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return engine, home


def test_recipe_dir_prefers_the_workflow_then_the_project_then_the_engine(engine_and_home):
    engine, home = engine_and_home
    d = _project_recipe(home)
    assert recipe_dir("my-check", engine, str(d / "workflow.yaml")) == d
    assert recipe_dir("my-check", engine) == d
    assert recipe_dir("my_check", engine) == d
    assert recipe_dir("absent", engine) is None


def test_static_plan_reads_the_project_recipes_contract(engine_and_home):
    engine, home = engine_and_home
    d = _project_recipe(home)
    plan = json.loads(recipe_fallback_plan("my-check", str(d / "workflow.yaml"), str(engine), "k.md"))
    assert plan["artifact_contract"]["success_verifiers"] == ["verifiers/probe.py"]


def test_overlay_plan_reads_the_project_recipes_contract(engine_and_home, tmp_path):
    engine, home = engine_and_home
    _project_recipe(home)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"recipe": "my-check"}))
    out = json.loads(overlay_plan(json.dumps({"decomposition": []}), "my_check", str(profile), str(engine)))
    assert out["artifact_contract"]["success_verifiers"] == ["verifiers/probe.py"]


def test_verify_finds_a_project_recipes_verifier(engine_and_home, monkeypatch):
    engine, home = engine_and_home
    d = _project_recipe(home)
    monkeypatch.setenv("MINI_ORK_RECIPE", "my-check")
    assert verify._find_verifier_script("verifiers/probe.py", str(engine), str(home)) == str(d / "verifiers" / "probe.py")
