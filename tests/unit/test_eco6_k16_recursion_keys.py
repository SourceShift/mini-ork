"""ECOSYSTEM §6 · K16: a ``recursion:`` block declares only the two caps a
reader honors.

``budget_cap_per_iter_usd``, ``convergence_check`` and ``divergence_kill`` were
exported as ``MO_RECURSION_*`` and read by nothing. This file pins their
removal across the loader, the schema, the five shipped recipes, the executor's
env publication, and the operator doc — and proves the two surviving values did
not move.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import jsonschema
import pytest
import yaml

from mini_ork.workflow.compiler import compile_workflow
from mini_ork.workflow.recursion import (
    FIELDS,
    RecursionConfig,
    RecursionConfigError,
    load_recursion_config,
)

REPO = Path(__file__).resolve().parents[2]

KEPT = ("max_iterations", "budget_cap_total_usd")
REMOVED = ("budget_cap_per_iter_usd", "convergence_check", "divergence_kill")
REMOVED_EXPORTS = (
    "MO_RECURSION_CONVERGENCE_CHECK",
    "MO_RECURSION_BUDGET_CAP_PER_ITER_USD",
    "MO_RECURSION_DIVERGENCE_KILL",
)
KEPT_EXPORTS = ("MO_RECURSION_MAX_ITERATIONS", "MO_RECURSION_BUDGET_CAP_TOTAL_USD")

RECIPES = (
    "goal-loop",
    "doc-to-features-loop",
    "prompt-graph-loop",
    "recursive-validate-impl",
    "spec-driven-delivery",
)

# The declared values before the removal, used only when git HEAD is unreadable.
FALLBACK = {
    "goal-loop": (30, 150.0),
    "doc-to-features-loop": (8, 100.0),
    "prompt-graph-loop": (5, 20.0),
    "recursive-validate-impl": (5, 60.0),
    "spec-driven-delivery": (6, 100.0),
}


def _workflow(recipe: str) -> Path:
    return REPO / "recipes" / recipe / "workflow.yaml"


def _schema() -> dict:
    return json.loads((REPO / "schemas" / "workflow.schema.json").read_text())


def _head_recursion(recipe: str) -> dict | None:
    """The recipe's recursion block at HEAD — i.e. before this change.

    The verifier builds its patched copy from ``pre-implementer-ref`` and
    commits that snapshot, so ``HEAD`` is the pre-change recipe on both sides.
    """
    try:
        raw = subprocess.run(
            ["git", "-C", str(REPO), "show", f"HEAD:recipes/{recipe}/workflow.yaml"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    loaded = yaml.safe_load(raw) or {}
    block = loaded.get("recursion")
    return block if isinstance(block, dict) else None


def _write(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text("recursion:\n" + "".join(f"  {line}\n" for line in lines))
    return path


# ── the loader keeps exactly the two keys ─────────────────────────────────


def test_fields_are_the_two_kept_keys() -> None:
    assert FIELDS == KEPT


def test_config_has_only_the_two_kept_fields() -> None:
    assert {field.name for field in dataclasses.fields(RecursionConfig)} == set(KEPT)


@pytest.mark.parametrize("recipe", RECIPES)
def test_shipped_recipe_loads_exactly_two_kept_values(recipe: str) -> None:
    config = load_recursion_config(_workflow(recipe))
    assert isinstance(config, RecursionConfig)
    assert {field.name for field in dataclasses.fields(config)} == set(KEPT)

    declared = _head_recursion(recipe)
    expected = (
        (declared["max_iterations"], declared["budget_cap_total_usd"])
        if declared
        else FALLBACK[recipe]
    )
    assert (config.max_iterations, config.budget_cap_total_usd) == expected


@pytest.mark.parametrize("recipe", RECIPES)
def test_shipped_recipe_block_declares_only_two_keys(recipe: str) -> None:
    block = yaml.safe_load(_workflow(recipe).read_text())["recursion"]
    assert set(block) == set(KEPT)


@pytest.mark.parametrize("removed", REMOVED)
def test_removed_key_raises_a_removal_message(tmp_path: Path, removed: str) -> None:
    value = "1.0" if removed == "budget_cap_per_iter_usd" else "some preserved prose"
    path = _write(
        tmp_path,
        [
            "max_iterations: 3",
            "budget_cap_total_usd: 12.0",
            f"{removed}: {value}",
        ],
    )
    with pytest.raises(RecursionConfigError) as excinfo:
        load_recursion_config(path)
    message = str(excinfo.value)
    assert f"recursion.{removed} was removed in 2026-10" in message, message
    assert "delete it from" in message, message
    assert str(path) in message, message


def test_missing_budget_cap_total_usd_is_named(tmp_path: Path) -> None:
    path = _write(tmp_path, ["max_iterations: 3"])
    with pytest.raises(RecursionConfigError, match="budget_cap_total_usd"):
        load_recursion_config(path)


# ── the schema declares the same two keys, both required ──────────────────


def test_schema_recursion_object_is_two_required_properties() -> None:
    block = _schema()["properties"]["recursion"]
    assert set(block["properties"]) == set(KEPT)
    assert set(block["required"]) == set(KEPT)
    assert block["additionalProperties"] is False


@pytest.mark.parametrize("recipe", RECIPES)
def test_recipe_compiles_and_validates(recipe: str) -> None:
    path = _workflow(recipe)
    jsonschema.validate(instance=yaml.safe_load(path.read_text()), schema=_schema())
    compiled = compile_workflow(path)
    assert compiled is not None
    assert compiled.nodes


# ── the executor and the operator doc publish/document only the two ───────


def test_execute_publishes_only_the_two_kept_exports() -> None:
    text = (REPO / "mini_ork" / "cli" / "execute.py").read_text()
    for name in REMOVED_EXPORTS:
        assert name not in text, f"{name} still present in execute.py"
    for name in KEPT_EXPORTS:
        assert name in text, f"{name} must remain in execute.py"


def test_feature_flags_doc_names_only_the_two_kept_rows() -> None:
    doc = (REPO / "docs" / "operator" / "feature-flags.md").read_text()
    for name in REMOVED_EXPORTS:
        assert name not in doc, f"{name} row still present in feature-flags.md"
    for name in KEPT_EXPORTS:
        assert name in doc, f"{name} row must remain in feature-flags.md"
    assert any(
        "max_iterations" in line and "budget_cap_total_usd" in line
        for line in doc.splitlines()
    ), "no sentence says a recursion: block declares only the two kept keys"
