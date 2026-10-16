"""Every ``mini_ork.ide_pages`` module imports.

The page modules are loaded lazily — ``board_cmd`` imports the one page a read
names — so a module that no longer imports is invisible until someone opens
that page. That is how a feature removal left ``node.py`` importing a symbol
``run.py`` had dropped: the board, the run page and the rest of the suite
stayed green (the tests covering the affected page could not even be
collected), and the first symptom was a traceback in the IDE's node tab.

This walk names the module that broke, which is the one thing the lazy load
cannot tell you.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import mini_ork.ide_pages as pages

MODULES = sorted(module.name for module in pkgutil.iter_modules(pages.__path__))


def test_the_page_package_has_modules_to_check() -> None:
    """A walk over nothing passes silently — an empty package is a broken one."""
    assert MODULES


@pytest.mark.parametrize("name", MODULES)
def test_page_module_imports(name: str) -> None:
    importlib.import_module(f"mini_ork.ide_pages.{name}")
