"""Lock the `recursion` block of workflow.schema.json to its observed 2-key shape."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO / "schemas" / "workflow.schema.json").read_text())
RECURSION_SUB = SCHEMA["properties"]["recursion"]

RECIPES = [
    "doc-to-features-loop",
    "goal-loop",
    "prompt-graph-loop",
    "recursive-validate-impl",
]


@pytest.mark.parametrize("recipe", RECIPES)
def test_recipe_recursion_validates(recipe: str) -> None:
    wf = yaml.safe_load((REPO / "recipes" / recipe / "workflow.yaml").read_text())
    jsonschema.validate(instance=wf["recursion"], schema=RECURSION_SUB)


def test_typo_key_rejected() -> None:
    base = {
        "max_iterations": 5,
        "budget_cap_total_usd": 5.0,
    }
    bad = {**base, "budget_cap_total": 5}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=RECURSION_SUB)


def test_zero_iterations_rejected() -> None:
    bad = {"max_iterations": 0, "budget_cap_total_usd": 5.0}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=RECURSION_SUB)
