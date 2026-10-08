"""Recipe-local registrations for the registry-audit recipe.

Loaded once per process via ``mini_ork.cli.recipe_register.load_recipe_register``,
which executes module-level side effects with
``importlib.util.spec_from_file_location``. Every import here must be safe when
``mini_ork`` is importable and must not reach for a missing dependency at module
top level.

What this module does:

1. Eagerly loads ``recipes/registry-audit/lib/transforms.py`` by file path so
   its two ``@register_transform`` decorators fire and register
   ``registry_parse`` / ``audit_plan`` in
   ``mini_ork.workflow.transforms._TRANSFORMS``. Without this the
   ``compile_workflow`` step raises ``ArtifactContractError: unknown artifact
   transform: registry_parse`` because the type:transform nodes in
   workflow.yaml name them. File-path loading is required because ``recipes/``
   is not a Python package (no ``recipes/__init__.py``).

2. Registers the ``(registry-audit, audit_items)`` implementer submode with the
   fan-out driver (``lib/fanout_driver.py``). Invoked by the dispatch layer with
   no args, the driver reads ``<run_dir>/audit-plan.json``, runs the shared
   bounded pool from ``mini_ork.orchestration.item_fanout`` over the planned
   items, and writes ``<run_dir>/audit-result.json``. ``MO_REGISTRY_DRY=1``
   records the plan without spawning (unit-test seam).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from mini_ork.cli.execute_handlers import register_implementer_submode

_RECIPE_DIR = Path(__file__).resolve().parent
# Relative-from-recipes path; the dispatcher joins it onto the repo root
# (``os.path.join(ctx.root, "recipes", script_rel)``).
_DRIVER_SCRIPT = "registry-audit/lib/fanout_driver.py"
_TRANSFORMS_PATH = _RECIPE_DIR / "lib" / "transforms.py"

# The recipe-local loader runs this file under a synthetic module name with no
# parent package, so ``from .lib.transforms import ...`` would fail. Load by
# file path, mirroring mini_ork/cli/recipe_register.py:67.
_TRANSFORMS_SPEC = importlib.util.spec_from_file_location(
    "registry_audit_transforms", _TRANSFORMS_PATH,
)
if _TRANSFORMS_SPEC is None or _TRANSFORMS_SPEC.loader is None:
    raise ImportError(f"could not load transforms module from {_TRANSFORMS_PATH}")
_TRANSFORMS_MODULE = importlib.util.module_from_spec(_TRANSFORMS_SPEC)
sys.modules.setdefault(_TRANSFORMS_SPEC.name, _TRANSFORMS_MODULE)
_TRANSFORMS_SPEC.loader.exec_module(_TRANSFORMS_MODULE)

register_implementer_submode(
    recipe="registry-audit",
    node_id="audit_items",
    results_artifact="audit-result.json",
    script=_DRIVER_SCRIPT,
)
