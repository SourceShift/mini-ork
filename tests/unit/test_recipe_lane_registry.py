"""Every ``model_lane`` a committed recipe references must resolve to a real lane.

A recipe's ``workflow.yaml`` names its lanes via ``node.model_lane``. The engine
resolves that string through the tracked lane registries — ``.mini-ork/config/
agents.yaml`` (the operative home registry) and ``config/agents.yaml`` (the repo
template ``mini-ork init`` copies into a fresh home) — and otherwise treats it
as a plain provider model name (``config/providers.yaml``). A lane that is
neither is dispatched verbatim; ``*_lens`` aliases are NOT providers.yaml keys,
so an unresolved one fails dispatch preflight and the ``MO_FALLBACK_*`` tail
silently decides who actually serves (the codex_lens -> minimax bug). A bare
provider model name such as ``codex`` resolves on its own and is therefore fine.

This is the regression guard for ``recipes/audit-judge-panel-3l``, whose third
judge runs on ``laguna_lens`` — an alias that was missing from both tracked
registries. Adding it there is what makes this test pass; a recipe that names a
lens alias no registry defines fails it, with the recipe and lane named.

The scan reads ``model_lane:`` tokens line-by-line. Lanes appear inside flow
mappings (``- {name: …, model_lane: …}``) the workflow schema does not
otherwise constrain, so a full YAML parse of every workflow is neither needed
nor wanted.

One documented exception: ``recipes/chapter-validation-10lens`` uses recipe-local
numeric lanes ``lens_01``..``lens_10``. Its README declares those as operator-
supplied entries the deployer adds to their own ``$MINI_ORK_HOME/config/
agents.yaml``, so they are not expected in the two tracked files.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RECIPES = REPO / "recipes"

# Tracked lane registries, both of which are consulted at dispatch time.
LANE_REGISTRIES = (
    REPO / ".mini-ork" / "config" / "agents.yaml",
    REPO / "config" / "agents.yaml",
)
# Provider model names a lane may name directly instead of via an alias.
PROVIDERS = REPO / "config" / "providers.yaml"

# ``model_lane:`` followed by the lane token, anywhere on the line so both the
# block style (``    model_lane: worker``) and the flow style
# (``- {name: x, model_lane: x}``) are caught. The token stops at the first
# character that cannot be part of a lane (comma, quote, brace, newline).
_MODEL_LANE = re.compile(r"model_lane:\s*([A-Za-z0-9_.\-]+)")
# Recipe-local numeric lens lanes are operator-supplied, not tracked (above).
_RECIPE_LOCAL_LANE = re.compile(r"lens_\d+$")


def _keys(path: Path, section: str) -> set[str]:
    """The top-level keys of ``section`` in the YAML document at ``path``."""
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = document.get(section) or {}
    assert isinstance(block, dict), f"{path}: '{section}' must be a mapping"
    return set(block)


def _referenced_lanes() -> list[tuple[str, str]]:
    """``(recipe, lane)`` for every model_lane token in a committed recipe."""
    found: list[tuple[str, str]] = []
    for workflow in sorted(RECIPES.glob("*/workflow.yaml")):
        for lane in _MODEL_LANE.findall(workflow.read_text(encoding="utf-8")):
            found.append((workflow.parent.name, lane))
    return found


def test_recipes_reference_resolvable_lanes() -> None:
    resolvable = set()
    for registry in LANE_REGISTRIES:
        resolvable |= _keys(registry, "lanes")
    resolvable |= _keys(PROVIDERS, "providers")

    unresolved = [
        (recipe, lane)
        for recipe, lane in _referenced_lanes()
        if lane not in resolvable and not _RECIPE_LOCAL_LANE.search(lane)
    ]

    assert not unresolved, (
        "recipe model_lane values that resolve in neither a tracked lane "
        "registry nor config/providers.yaml: "
        + ", ".join(f"{recipe}: {lane}" for recipe, lane in unresolved)
    )
