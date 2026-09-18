"""Loader for recipe-local ``register.py`` modules.

Recipes may ship an optional ``<recipe_dir>/register.py`` that self-registers
extensions (node handlers, implementer submodes, routing policies, gate
evaluators) via the existing public ``register_*`` APIs. The loader executes
the module as a file-path import (``importlib.util.spec_from_file_location``)
and returns ``True``; an absent ``register.py`` is a silent no-op that returns
``False``.

Idempotency: per process, each resolved ``register.py`` path is executed at
most once. Subsequent calls return ``True`` without re-executing.

Loud failure: any exception during execution (``SyntaxError``, ``ImportError``,
runtime error) raises :class:`RecipeRegisterError` with the original exception
chained via ``raise ... from exc``. A broken ``register.py`` MUST fail the run
with ``rc != 0``; the loader NEVER silently swallows errors.

Security note: a recipe-local ``register.py`` can call
``register_node_handler(node_type, ...)`` to overwrite a CORE handler for the
remainder of the process. The loader cannot defend against that; recipe authors
MUST audit the contents of their ``register.py``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_LOADED: set[Path] = set()


class RecipeRegisterError(RuntimeError):
    """Raised when a recipe's ``register.py`` cannot be loaded.

    The original exception is chained via ``__cause__``; inspect it to find
    the underlying ``SyntaxError`` / ``ImportError`` / runtime error.
    """


def load_recipe_register(recipe_dir: Path) -> bool:
    """Load ``<recipe_dir>/register.py`` exactly once per process per resolved path.

    Args:
        recipe_dir: directory of the recipe whose ``register.py`` should be
            loaded. The directory is not required to exist; an absent
            ``register.py`` is a silent no-op.

    Returns:
        ``True`` when ``register.py`` is present and either executed now or
        already loaded earlier in this process. ``False`` when no
        ``register.py`` ships with the recipe.

    Raises:
        RecipeRegisterError: ``register.py`` exists but failed to execute. The
            original exception is chained via ``__cause__`` and is never
            silently swallowed.
    """
    register_path = Path(recipe_dir) / "register.py"
    if not register_path.is_file():
        return False

    target = register_path.resolve()
    if target in _LOADED:
        return True

    try:
        spec = importlib.util.spec_from_file_location(
            f"_recipe_register_{target}", target,
        )
    except Exception as exc:
        raise RecipeRegisterError(
            f"recipe register loader: cannot build spec for {target}",
        ) from exc
    if spec is None or spec.loader is None:
        raise RecipeRegisterError(
            f"recipe register loader: spec_from_file_location returned None for {target}",
        )

    module = importlib.util.module_from_spec(spec)
    # Decorators such as @dataclass resolve their owning module via
    # sys.modules while the file is executing — mirror bin/mini-ork's
    # _bootstrap_install() precedent so cross-imports inside register.py
    # resolve correctly.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise RecipeRegisterError(
            f"recipe register loader: failed to execute {target}",
        ) from exc

    _LOADED.add(target)
    return True
