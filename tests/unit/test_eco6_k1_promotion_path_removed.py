"""ECOSYSTEM §6 · K1: the dead improve → eval → promote path is gone.

Hermetic guards for the removal described in the kickoff. Module names are
assembled at runtime so this file itself never contains the strings the
kickoff's ``git grep`` criterion must report as absent.
"""

import importlib
import importlib.util
from pathlib import Path

import mini_ork

REPO_ROOT = Path(mini_ork.__file__).resolve().parent.parent

REMOVED_SUBS = ("improve", "eval", "promote")
REMOVED_MODULES = tuple(
    "mini_ork.cli." + name for name in REMOVED_SUBS
) + ("mini_ork.learning." + "group_" + "evolver",)
REMOVED_LAUNCHERS = tuple("bin/mini-ork-" + name for name in REMOVED_SUBS)
KEPT_LAUNCHERS = ("bin/mini-ork-recipe-eval", "bin/mini-ork-self-improve")


def test_removed_modules_are_not_importable():
    for module in REMOVED_MODULES:
        assert importlib.util.find_spec(module) is None, module


def test_kept_modules_still_import():
    importlib.import_module("mini_ork.learning.benchmark_suite")
    importlib.import_module("mini_ork.gates.promotion_gate")


def test_removed_subs_are_not_dispatchable():
    import mini_ork.cli.main as cli_main

    for name in REMOVED_SUBS:
        assert name not in cli_main._NATIVE_MODULE_SUBS, name
        assert name not in cli_main.SUBCOMMAND_REGISTRY, name


def test_help_has_no_promotion_path_lines():
    import mini_ork.cli.main as cli_main

    for line in cli_main._HELP.splitlines():
        assert not line.startswith("  improve"), repr(line)
        assert not line.startswith("  eval --candidate"), repr(line)
        assert not line.startswith("  promote --candidate"), repr(line)


def test_apply_still_dispatchable():
    import mini_ork.cli.main as cli_main

    assert ("apply" in cli_main._NATIVE_SUBS) or (
        "apply" in cli_main.SUBCOMMAND_REGISTRY
    )


def test_removed_launchers_are_gone_kept_launchers_remain():
    for rel in REMOVED_LAUNCHERS:
        assert not (REPO_ROOT / rel).exists(), rel
    for rel in KEPT_LAUNCHERS:
        assert (REPO_ROOT / rel).exists(), rel


def test_no_source_file_references_the_removed_evolver():
    needle = "group_" + "evolver"
    offenders = []
    for py in sorted((REPO_ROOT / "mini_ork").rglob("*.py")):
        if needle in py.read_text(encoding="utf-8", errors="replace"):
            offenders.append(str(py.relative_to(REPO_ROOT)))
    assert not offenders, offenders
