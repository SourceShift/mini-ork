"""Unit tests: ``mini_ork.workflow.recursion.load_recursion_config``.

The declared ``recursion:`` block used to be enforced by the schema and read by
nobody, so a recipe's numbers and the driver's hardcoded defaults drifted apart
in silence. These tests pin the reader that ends that: the four recipes that
declare a block load their exact declared values, a recipe with no block reports
``None`` rather than inventing defaults, and a malformed block raises instead of
half-applying.

No mocks — every case runs against a real YAML file on disk.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mini_ork.workflow.recursion import (
    FIELDS,
    RecursionConfig,
    RecursionConfigError,
    load_recursion_config,
)

REPO = Path(__file__).resolve().parents[2]

# Declared blocks, transcribed from the recipes. Pinned literally rather than
# re-read from the file: a test that computes its expectation from the same
# source it is testing asserts nothing.
DECLARED = {
    "doc-to-features-loop": {
        "max_iterations": 8,
        "convergence_check": "all_p0_features_passed",
        "budget_cap_per_iter_usd": 15.0,
        "budget_cap_total_usd": 100.0,
    },
    "goal-loop": {
        "max_iterations": 30,
        "convergence_check": "all_goal_units_pass",
        "budget_cap_per_iter_usd": 10.0,
        "budget_cap_total_usd": 150.0,
    },
    "prompt-graph-loop": {
        "max_iterations": 5,
        "convergence_check": "human_decision_approved_and_aggregation_complete",
        "budget_cap_per_iter_usd": 4.0,
        "budget_cap_total_usd": 20.0,
    },
    "recursive-validate-impl": {
        "max_iterations": 5,
        "convergence_check": "all_dod_probes_pass",
        "budget_cap_per_iter_usd": 5.0,
        "budget_cap_total_usd": 25.0,
    },
}


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _block(**overrides) -> dict:
    base = {
        "max_iterations": 5,
        "convergence_check": "check",
        "budget_cap_per_iter_usd": 1.0,
        "budget_cap_total_usd": 5.0,
        "divergence_kill": "kill",
    }
    base.update(overrides)
    return base


# ── declared recipes load their exact values ──────────────────────────────


@pytest.mark.parametrize("recipe", sorted(DECLARED))
def test_declared_recipe_round_trips(recipe: str) -> None:
    config = load_recursion_config(REPO / "recipes" / recipe / "workflow.yaml")

    assert config is not None
    expected = DECLARED[recipe]
    assert config.max_iterations == expected["max_iterations"]
    assert config.convergence_check == expected["convergence_check"]
    assert config.budget_cap_per_iter_usd == expected["budget_cap_per_iter_usd"]
    assert config.budget_cap_total_usd == expected["budget_cap_total_usd"]
    # divergence_kill is free prose (multi-line, trailing newline in every
    # declaring recipe); assert it survives verbatim rather than normalized.
    assert config.divergence_kill.strip()
    assert config.divergence_kill == yaml.safe_load(
        (REPO / "recipes" / recipe / "workflow.yaml").read_text()
    )["recursion"]["divergence_kill"]


def test_config_is_frozen() -> None:
    """The config is a value object: nothing downstream may mutate the declaration."""
    config = load_recursion_config(
        REPO / "recipes" / "goal-loop" / "workflow.yaml"
    )
    assert isinstance(config, RecursionConfig)
    with pytest.raises(Exception):
        config.max_iterations = 999  # type: ignore[misc]


# ── absence ───────────────────────────────────────────────────────────────


def test_recipe_without_block_returns_none(tmp_path: Path) -> None:
    path = _write(tmp_path, {"version": "0.1.0", "nodes": [], "edges": []})
    assert load_recursion_config(path) is None


def test_explicit_null_block_returns_none(tmp_path: Path) -> None:
    """``recursion:`` with no value is absence, not a malformed block."""
    path = _write(tmp_path, {"recursion": None, "nodes": []})
    assert load_recursion_config(path) is None


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RecursionConfigError, match="workflow not found"):
        load_recursion_config(tmp_path / "nope.yaml")


# ── malformed blocks raise rather than half-apply ─────────────────────────


def test_unknown_key_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"recursion": _block(budget_cap_total=5)})
    with pytest.raises(RecursionConfigError, match="unknown recursion key"):
        load_recursion_config(path)


def test_partial_block_raises(tmp_path: Path) -> None:
    """The schema allows this (no `required` array); the loader must not.

    A partial block that quietly picks up a hardcoded default is the exact
    "edit the YAML and nothing happens" bug this reader exists to fix.
    """
    path = _write(tmp_path, {"recursion": {"max_iterations": 5}})
    with pytest.raises(RecursionConfigError, match="missing"):
        load_recursion_config(path)


def test_bool_is_not_an_integer(tmp_path: Path) -> None:
    """``True`` is an ``int`` in Python; it must not read as max_iterations=1."""
    path = _write(tmp_path, {"recursion": _block(max_iterations=True)})
    with pytest.raises(RecursionConfigError, match="must be an integer"):
        load_recursion_config(path)


def test_zero_iterations_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"recursion": _block(max_iterations=0)})
    with pytest.raises(RecursionConfigError, match=">= 1"):
        load_recursion_config(path)


def test_negative_budget_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"recursion": _block(budget_cap_total_usd=-1)})
    with pytest.raises(RecursionConfigError, match=">= 0"):
        load_recursion_config(path)


def test_empty_convergence_check_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"recursion": _block(convergence_check="   ")})
    with pytest.raises(RecursionConfigError, match="non-empty string"):
        load_recursion_config(path)


def test_non_mapping_block_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, {"recursion": ["not", "an", "object"]})
    with pytest.raises(RecursionConfigError, match="must be an object"):
        load_recursion_config(path)


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "workflow.yaml"
    path.write_text("recursion: [unclosed\n", encoding="utf-8")
    with pytest.raises(RecursionConfigError, match="invalid workflow YAML"):
        load_recursion_config(path)


# ── coercion ──────────────────────────────────────────────────────────────


def test_integer_budget_coerces_to_float(tmp_path: Path) -> None:
    """YAML ``5`` is an int; the schema says ``number``, so 5.0 must load."""
    path = _write(tmp_path, {"recursion": _block(budget_cap_total_usd=5)})
    config = load_recursion_config(path)
    assert config is not None
    assert isinstance(config.budget_cap_total_usd, float)
    assert config.budget_cap_total_usd == 5.0


def test_field_order_matches_schema() -> None:
    """FIELDS mirrors the five schema properties, in schema order."""
    assert FIELDS == (
        "max_iterations",
        "convergence_check",
        "budget_cap_per_iter_usd",
        "budget_cap_total_usd",
        "divergence_kill",
    )


# ── the chain: declared YAML → published env → driver default ─────────────


def test_declared_block_reaches_the_driver() -> None:
    """The whole point of the change, asserted end to end.

    A recipe's YAML is read, published as ``MO_RECURSION_*`` exactly the way
    ``mini_ork/cli/execute.py`` publishes it, and then resolved by the driver's
    own default path with no argument passed. If any link breaks — the reader
    stops seeing the block, an env key is renamed, the driver stops consulting
    it — this fails, which is the failure a recipe previously could not detect.

    ``prompt-graph-loop`` is used deliberately: it declares 5 / 20.00, which
    differ from the driver's historical literals (30 / 150.0), so a pass here
    cannot be the fallback masquerading as the declaration.
    """
    import importlib.util
    import os
    import sys as _sys

    from mini_ork.context import publish_env

    drive_py = REPO / "recipes" / "goal-loop" / "lib" / "drive.py"
    spec = importlib.util.spec_from_file_location("recursion_chain_drive", drive_py)
    assert spec is not None and spec.loader is not None
    drive_mod = importlib.util.module_from_spec(spec)
    _sys.modules.setdefault(spec.name, drive_mod)
    spec.loader.exec_module(drive_mod)

    recipe = "prompt-graph-loop"
    config = load_recursion_config(REPO / "recipes" / recipe / "workflow.yaml")
    assert config is not None
    assert (config.max_iterations, config.budget_cap_total_usd) == (5, 20.0)

    # Mirror execute.py's publish exactly (keys and formatting).
    publish_env({
        "MO_RECURSION_MAX_ITERATIONS": str(config.max_iterations),
        "MO_RECURSION_CONVERGENCE_CHECK": config.convergence_check,
        "MO_RECURSION_BUDGET_CAP_PER_ITER_USD": f"{config.budget_cap_per_iter_usd:.2f}",
        "MO_RECURSION_BUDGET_CAP_TOTAL_USD": f"{config.budget_cap_total_usd:.2f}",
        "MO_RECURSION_DIVERGENCE_KILL": config.divergence_kill,
    })

    # Resolved through the driver's own helpers, with the literals as defaults
    # so a broken chain would show as 30 / 150.0 rather than as an exception.
    assert drive_mod._env_int("MO_RECURSION_MAX_ITERATIONS", 30) == 5  # noqa: SLF001
    assert drive_mod._env_float("MO_RECURSION_BUDGET_CAP_TOTAL_USD", 150.0) == 20.0  # noqa: SLF001
    assert drive_mod._env_float("MO_RECURSION_BUDGET_CAP_PER_ITER_USD", 0.0) == 4.0  # noqa: SLF001
    # Check strings travel verbatim.
    assert (
        os.environ["MO_RECURSION_CONVERGENCE_CHECK"]
        == "human_decision_approved_and_aggregation_complete"
    )
