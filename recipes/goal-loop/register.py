"""Recipe-local registrations for the goal-loop recipe.

Loaded once per process via ``mini_ork.cli.recipe_register.load_recipe_register``,
which executes module-level side effects via ``importlib.util.spec_from_file_location``.
All imports must be import-safe when ``mini_ork`` is importable — never reach
for a missing dependency at module top level.

What this module does:

1. Loads ``recipes/goal-loop/lib/transforms.py`` by file path so the two
   ``@register_transform("goal_state_eval")`` and
   ``@register_transform("goal_sweep_plan")`` decorators fire and register
   their handlers in ``mini_ork.workflow.transforms._TRANSFORMS``. Without
   this eager load, ``compile_workflow`` would raise
   ``ArtifactContractError: unknown artifact transform: <id>`` because the
   type:transform nodes in workflow.yaml declare ``transform: <id>``.
   File-path loading is required because ``recipes/`` is not a Python
   package (``recipes/__init__.py`` does not exist and the kickoff scope
   forbids touching files outside ``recipes/goal-loop/`` + the one test
   file).

2. Registers the ``(goal-loop, sweep_dispatcher)`` implementer submode with
   a stub dispatcher script (``lib/goal_sweep_stub.py``) so the
   recipe-local loader contract is dogfooded end-to-end. U4b will replace
   this stub with the real fan-out driver.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from mini_ork.cli.execute_handlers import register_implementer_submode

_RECIPE_DIR = Path(__file__).resolve().parent
_STUB_SCRIPT = str(_RECIPE_DIR / "lib" / "goal_sweep_stub.py")
_TRANSFORMS_PATH = _RECIPE_DIR / "lib" / "transforms.py"

# Eagerly load the transforms module so the @register_transform decorators
# register their handlers in mini_ork.workflow.transforms._TRANSFORMS. The
# recipe_local loader uses importlib.util.spec_from_file_location to run
# this file — a synthetic module name that does NOT carry parent package
# context — so a normal ``from .lib.transforms import ...`` would fail with
# "no ``Package`` parent". The file-path load mirrors the recipe_register
# loader pattern at ``mini_ork/cli/recipe_register.py:67``.
_TRANSFORMS_SPEC = importlib.util.spec_from_file_location(
    "goal_loop_transforms", _TRANSFORMS_PATH,
)
if _TRANSFORMS_SPEC is None or _TRANSFORMS_SPEC.loader is None:
    raise ImportError(f"could not load transforms module from {_TRANSFORMS_PATH}")
_TRANSFORMS_MODULE = importlib.util.module_from_spec(_TRANSFORMS_SPEC)
sys.modules.setdefault(_TRANSFORMS_SPEC.name, _TRANSFORMS_MODULE)
_TRANSFORMS_SPEC.loader.exec_module(_TRANSFORMS_MODULE)

register_implementer_submode(
    recipe="goal-loop",
    node_id="sweep_dispatcher",
    results_artifact="sweep-result.json",
    script=_STUB_SCRIPT,
)