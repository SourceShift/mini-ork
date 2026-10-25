"""ECOSYSTEM §6 K3: workflow-schema validation and task-class uniqueness.

Hermetic: the recipe/schema assertions read this checkout; the validate CLI
cases build their own tmp ``MINI_ORK_ROOT``/``MINI_ORK_HOME`` so no network,
credentials, or live engine state is touched.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml

from mini_ork.cli import validate

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "schemas" / "workflow.schema.json"
RECIPES_DIR = REPO / "recipes"

RECIPE_NAMES = sorted(
    p.name for p in RECIPES_DIR.iterdir()
    if p.is_dir() and (p / "workflow.yaml").is_file()
)


def _validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.validators.validator_for(schema)(schema)


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _error_path(error) -> str:
    return "/".join(str(part) for part in error.absolute_path) or "<root>"


# ── every shipped recipe satisfies the workflow schema ───────────────────────

@pytest.mark.parametrize("recipe", RECIPE_NAMES)
def test_every_shipped_recipe_passes_workflow_schema(recipe: str) -> None:
    doc = _load(RECIPES_DIR / recipe / "workflow.yaml")
    errors = list(_validator().iter_errors(doc))
    assert not errors, (
        f"recipes/{recipe}/workflow.yaml violates the schema: "
        + "; ".join(f"{_error_path(e)}: {e.message}" for e in errors[:5])
    )


def test_workflow_schema_is_draft_2020_12() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert (
        jsonschema.validators.validator_for(schema)
        is jsonschema.Draft202012Validator
    )


# ── task classes are unique and consistent across recipes ────────────────────

def test_task_class_names_are_unique() -> None:
    names: dict[str, list[str]] = {}
    for recipe in RECIPE_NAMES:
        tc = RECIPES_DIR / recipe / "task_class.yaml"
        if not tc.is_file():
            continue
        names.setdefault(_load(tc)["name"], []).append(recipe)
    dupes = {name: rs for name, rs in names.items() if len(rs) > 1}
    assert not dupes, f"task classes declared by more than one recipe: {dupes}"


# The kickoff's "when present" means the manifest declares the *key*, not that
# the file exists: several shipped contracts predate it or use the legacy
# `recipe:` alias, and the kickoff pins every one of them out of scope. A
# differing declared class is drift; an absent key is not.

def test_task_class_declarations_are_consistent() -> None:
    for recipe in RECIPE_NAMES:
        tc = RECIPES_DIR / recipe / "task_class.yaml"
        if not tc.is_file():
            continue
        name = _load(tc)["name"]
        for manifest in ("workflow.yaml", "artifact_contract.yaml"):
            path = RECIPES_DIR / recipe / manifest
            if not path.is_file():
                continue
            declared = _load(path).get("task_class")
            if declared is None:
                continue
            assert declared == name, (
                f"recipes/{recipe}/{manifest} declares task_class "
                f"{declared!r} but task_class.yaml name is {name!r}"
            )


def test_legacy_recipe_alias_matches_task_class() -> None:
    """A contract on the pre-`task_class:` key must still name its own class."""
    for recipe in RECIPE_NAMES:
        path = RECIPES_DIR / recipe / "artifact_contract.yaml"
        tc = RECIPES_DIR / recipe / "task_class.yaml"
        if not path.is_file() or not tc.is_file():
            continue
        doc = _load(path)
        if "recipe" not in doc:
            continue
        assert doc["recipe"] == _load(tc)["name"], (
            f"recipes/{recipe}/artifact_contract.yaml declares recipe "
            f"{doc['recipe']!r} but task_class.yaml name is "
            f"{_load(tc)['name']!r}"
        )


# ── validate reports a schema violation ──────────────────────────────────────

def _tmp_engine(tmp_path: Path, recipe: str = "code-fix"):
    root = tmp_path / "root"
    home = tmp_path / "home"
    home.mkdir()
    shutil.copytree(RECIPES_DIR / recipe, root / "recipes" / recipe)
    shutil.copytree(REPO / "schemas", root / "schemas")
    return root, home


def test_validate_reports_schema_violation(tmp_path, monkeypatch, capsys) -> None:
    root, home = _tmp_engine(tmp_path)
    wf = root / "recipes" / "code-fix" / "workflow.yaml"
    doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
    doc["edges"][0]["bogus_key"] = 1
    wf.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))

    assert validate.main(["--recipe", "code-fix"]) == 1
    assert "violates the schema" in capsys.readouterr().err


# ── validate reports a duplicate task class ──────────────────────────────────

def test_validate_reports_duplicate_task_class(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "root"
    home = tmp_path / "home"
    home.mkdir()
    for recipe in ("recipe-a", "recipe-b"):
        d = root / "recipes" / recipe
        d.mkdir(parents=True)
        (d / "task_class.yaml").write_text("name: dup_class\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))

    assert validate.main([]) == 1
    assert "declared by 2 recipes" in capsys.readouterr().err


# ── classification still routes refactor kickoffs to refactor-audit ──────────

def test_refactor_kickoff_still_routes_to_refactor_audit(tmp_path) -> None:
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text(
        "# Kickoff\n\nPerform a refactor audit of the provider roster.\n",
        encoding="utf-8",
    )
    assert validate._infer_recipe(str(kickoff), str(REPO)) == "refactor-audit"
