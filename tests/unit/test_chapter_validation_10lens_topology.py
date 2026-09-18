"""Regression guard for chapter-validation-10lens dependency-aware dispatch.

Pins the compiled ``control_parents`` graph and per-node ``dispatch_mode``
discipline so the recipe can never silently regress to a serial
``edges: []`` + ``dispatch_mode: partitioned`` shape.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.workflow import compile_workflow  # noqa: E402

RECIPE = REPO / "recipes" / "chapter-validation-10lens"
WORKFLOW = RECIPE / "workflow.yaml"

LENS_NAMES = (
    "structure",
    "factuality",
    "voice_tone",
    "length_density",
    "forbidden_constructs",
    "markdown_format",
    "coverage",
    "coherence",
    "reader_contract",
    "synthesis_originality",
)
LENS_NODE_NAMES = frozenset(f"lens_{i:02d}_{name}" for i, name in enumerate(LENS_NAMES, start=1))
LENS_NODE_RE = re.compile(r"^lens_\d{2}_")


@pytest.fixture(scope="module")
def compiled_workflow():
    return compile_workflow(WORKFLOW)


def _raw_yaml():
    import yaml
    with WORKFLOW.open() as fh:
        return yaml.safe_load(fh)


def test_each_lens_control_parent_is_planner(compiled_workflow):
    for lens in LENS_NODE_NAMES:
        assert set(compiled_workflow.control_parents[lens]) == {"planner"}, lens


def test_synthesizer_waits_for_all_lenses(compiled_workflow):
    assert set(compiled_workflow.control_parents["synthesizer"]) == LENS_NODE_NAMES


def test_lens_outputs_complete_waits_for_synthesizer(compiled_workflow):
    assert set(compiled_workflow.control_parents["lens_outputs_complete"]) == {"synthesizer"}


def test_publisher_waits_for_lens_outputs_complete(compiled_workflow):
    assert set(compiled_workflow.control_parents["publisher"]) == {"lens_outputs_complete"}


def test_each_lens_dispatch_mode_is_parallel_without_partition_key():
    raw = _raw_yaml()
    lens_nodes = [n for n in raw["nodes"] if LENS_NODE_RE.match(n["name"])]
    assert {n["name"] for n in lens_nodes} == LENS_NODE_NAMES
    for node in lens_nodes:
        assert node["dispatch_mode"] == "parallel", node["name"]
        assert "partition_key" not in node, node["name"]