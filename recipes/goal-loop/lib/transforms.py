"""Deterministic transforms for the goal-loop recipe.

`goal_state_eval` runs the units lister + predicate command once per wave
inside the target repo and materializes the per-unit pass/fail map. The
predicate is invoked as argv (NOT shell) so a unit id cannot escape its
argument position.

`sweep_dispatch_plan` reads the goal-state map, picks up to
MO_GOAL_MAX_CHILDREN_PER_WAVE failing units, and writes the wave's
fix-children plan. U4a only PLANS — actual child spawning arrives with the
U4b driver.

Both transforms are decorated with ``@register_transform`` so workflow.yaml
can name them in the ``transform:`` field of type:transform nodes. They run
inside the MiniOrk Python process (NOT inside a coding harness), keeping the
subprocess I/O reproducible and inspectable from the receipt layer.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

from mini_ork.workflow.artifacts import ArtifactContractError, ArtifactLedger
from mini_ork.workflow.compiler import CompiledWorkflow
from mini_ork.workflow.transforms import register_transform

# Load the goal_state helpers by file path because ``recipes/`` is not a
# Python package. The recipe-local loader uses
# ``importlib.util.spec_from_file_location`` which gives this module a
# synthetic name without a parent package, so a normal
# ``from .goal_state import ...`` would fail with "attempted relative import
# with no known parent package". Mirror the recipe_register loader pattern
# at ``mini_ork/cli/recipe_register.py:67``.
_GOAL_STATE_PATH = Path(__file__).resolve().parent / "goal_state.py"
_goal_state_spec = importlib.util.spec_from_file_location(
    "goal_loop_goal_state", _GOAL_STATE_PATH,
)
if _goal_state_spec is None or _goal_state_spec.loader is None:
    raise ImportError(f"could not load goal_state helper from {_GOAL_STATE_PATH}")
_goal_state_module = importlib.util.module_from_spec(_goal_state_spec)
sys.modules.setdefault(_goal_state_spec.name, _goal_state_module)
_goal_state_spec.loader.exec_module(_goal_state_module)
evaluate_units = _goal_state_module.evaluate_units
list_units = _goal_state_module.list_units


@register_transform("goal_state_eval")
def goal_state_eval(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Materialize ``goal-state.json`` from MO_GOAL_* env config.

    Inputs:
        ``MO_GOAL_TARGET_CWD`` — absolute path to the target repo.
        ``MO_GOAL_UNITS_CMD``  — command run inside target cwd, one unit id per line.
        ``MO_GOAL_PREDICATE_CMD`` — argv-prefix; ``<unit_id>`` appended per call.

    Output:
        ``<run_dir>/goal-state.json`` mapping ``unit_id -> {"pass": bool, "reason": str}``.
    """
    target_cwd = os.environ.get("MO_GOAL_TARGET_CWD")
    units_cmd = os.environ.get("MO_GOAL_UNITS_CMD")
    predicate_cmd = os.environ.get("MO_GOAL_PREDICATE_CMD")
    if not target_cwd or not units_cmd or not predicate_cmd:
        raise ArtifactContractError(
            "goal_state_eval requires MO_GOAL_TARGET_CWD, MO_GOAL_UNITS_CMD, "
            "and MO_GOAL_PREDICATE_CMD env vars",
        )

    units = list_units(target_cwd, units_cmd)
    states = evaluate_units(target_cwd, predicate_cmd, units)
    node = workflow.nodes[node_id]
    if "goal_state" not in node.outputs:
        raise ArtifactContractError("goal_state_eval requires a goal_state output")
    out_path = ledger.output_path(workflow, node_id, "goal_state")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(states, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


@register_transform("goal_sweep_plan")
def goal_sweep_plan(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Select failing units for the wave's fix children.

    Reads the upstream ``goal_state`` artifact written by ``goal_state_eval``,
    picks the failing units in deterministic order (sorted by unit_id), and
    caps the selection at ``MO_GOAL_MAX_CHILDREN_PER_WAVE`` (default 3).
    The output ``sweep-plan.json`` lists ``{unit_id, child_recipe, kickoff_hint}``
    entries — the outer U4b driver consumes this and dispatches the fix
    children. U4a only materializes the plan.
    """
    try:
        max_children = int(os.environ.get("MO_GOAL_MAX_CHILDREN_PER_WAVE", "3"))
    except ValueError:
        max_children = 3
    if max_children < 0:
        max_children = 0
    child_recipe = os.environ.get("MO_GOAL_CHILD_RECIPE", "")

    prepared = ledger.prepared_inputs(node_id)
    goal_state_paths = prepared.paths.get("goal_state", ())
    if not goal_state_paths:
        raise ArtifactContractError("goal_sweep_plan requires goal_state input")
    goal_state = json.loads(goal_state_paths[0].read_text(encoding="utf-8"))

    failing = sorted(
        unit_id for unit_id, state in goal_state.items() if not state.get("pass", False)
    )
    selected = failing[:max_children]

    node = workflow.nodes[node_id]
    if "sweep_plan" not in node.outputs:
        raise ArtifactContractError("goal_sweep_plan requires a sweep_plan output")
    out_path = ledger.output_path(workflow, node_id, "sweep_plan")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plan = [
        {
            "unit_id": unit_id,
            "child_recipe": child_recipe,
            "kickoff_hint": {
                "unit_id": unit_id,
                "reason": goal_state[unit_id].get("reason", ""),
            },
        }
        for unit_id in selected
    ]
    out_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path