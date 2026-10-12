"""Every shipped recipe's ``workflow.yaml`` must compile, and epic-runner's
reflector must stay wired into the graph.

The compiler is where edge declarations are checked: a ``retries`` edge whose
target is not a control-parent ancestor of its source is a hard error, and
``escalates_to`` edges never become control parents (``compiler.py``). So a
recipe can declare a node that no reachable edge ever orders, or add a loop
back edge that the compiler rejects, and nothing else in the suite notices —
the run simply dies at dispatch time or leaves the node pending forever.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.workflow import compile_workflow

RECIPES = Path(__file__).resolve().parents[2] / "recipes"
WORKFLOWS = sorted(RECIPES.glob("*/workflow.yaml"))


def test_the_recipe_directory_is_not_empty() -> None:
    # A glob that silently matched nothing would make the test below vacuous.
    assert len(WORKFLOWS) > 10, [str(w) for w in WORKFLOWS]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda w: w.parent.name)
def test_every_recipe_compiles(workflow: Path) -> None:
    compile_workflow(str(workflow))


def test_epic_runners_reflector_is_reachable_and_loops_back() -> None:
    compiled = compile_workflow(str(RECIPES / "epic-runner" / "workflow.yaml"))

    # Ordered: it waits for the verdict rather than running in the first wave.
    assert set(compiled.control_parents["reflector"]) == {"epic_verifier", "final_reviewer"}
    # And its failing reflection revises the dispatch wave instead of the run.
    assert compiled.retry_edges["reflector"].target == "epic_dispatcher"
    assert compiled.retry_edges["reflector"].max_rounds == 1
