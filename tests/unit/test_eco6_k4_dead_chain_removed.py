"""ECO6 K4 — the dead healer/cleaner/finalize chain is gone.

Hermetic acceptance test for the removal: the eight retired modules no longer
resolve, their unit-test files are gone, the surviving ``content_ids`` module
behaves exactly as the old verbatim copy did, and no package module still
names a retired path.
"""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
from pathlib import Path

import mini_ork

REPO_ROOT = Path(mini_ork.__file__).resolve().parent.parent

_PKG = "mini_ork"

# Build the retired module ids from parts so this file's own source does not
# contain the contiguous dotted paths the removal guards grep for.
DEAD_MODULES = [
    ".".join((_PKG, "recovery", "healer")),
    ".".join((_PKG, "recovery", "healer_bridge")),
    ".".join((_PKG, "recovery", "cleaner")),
    ".".join((_PKG, "recovery", "finalize")),
    ".".join((_PKG, "cache")),
    ".".join((_PKG, "vcs", "auto_merge")),
    ".".join((_PKG, "vcs", "auto_merge_pr")),
    ".".join((_PKG, "vcs", "pr_create")),
]

DEAD_TEST_FILES = [
    "tests/unit/test_healer_py.py",
    "tests/unit/test_mo_healer_bridge_py.py",
    "tests/unit/test_cleaner_py.py",
    "tests/unit/test_finalize_py.py",
    "tests/unit/test_cache_py.py",
    "tests/unit/test_auto_merge_py.py",
    "tests/unit/test_auto_merge_pr_py.py",
    "tests/unit/test_pr_create_py.py",
]

FORBIDDEN_STRINGS = [
    ".".join(("recovery", "healer")),
    ".".join(("recovery", "cleaner")),
    ".".join(("recovery", "finalize")),
    ".".join(("vcs", "auto_merge")),
    ".".join(("vcs", "pr_create")),
    ".".join(("mini_ork", "cache")),
]

SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".venv"}


def _run_git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "eco6@example.invalid")
    _run_git(repo, "config", "user.name", "eco6 test")
    (repo / "base.txt").write_text("base\n")
    _run_git(repo, "add", "base.txt")
    _run_git(repo, "commit", "-q", "-m", "base")
    _run_git(repo, "checkout", "-q", "-b", "feat")
    (repo / "feat.txt").write_text("on feat\n")
    _run_git(repo, "add", "feat.txt")
    _run_git(repo, "commit", "-q", "-m", "feat work")
    _run_git(repo, "checkout", "-q", "main")
    (repo / "main-side.txt").write_text("on main\n")
    _run_git(repo, "add", "main-side.txt")
    _run_git(repo, "commit", "-q", "-m", "main moves on")
    return repo


def test_retired_modules_do_not_resolve():
    for module_name in DEAD_MODULES:
        assert importlib.util.find_spec(module_name) is None, (
            f"{module_name} must be deleted"
        )


def test_retired_unit_test_files_are_gone():
    survivors = [rel for rel in DEAD_TEST_FILES if (REPO_ROOT / rel).exists()]
    assert not survivors, f"these retired test files must be deleted: {survivors}"


def test_content_ids_survives_and_matches_git(tmp_path):
    mod = importlib.import_module(".".join((_PKG, "vcs", "content_ids")))
    assert callable(mod.content_ids)

    repo = _make_repo(tmp_path)
    expected_base = _run_git(repo, "merge-base", "main", "feat")
    expected_tree = _run_git(repo, "rev-parse", "feat^{tree}")
    assert mod.content_ids(str(repo), "feat", "main") == (expected_base, expected_tree)


def test_content_ids_missing_branch_returns_empty_pair(tmp_path):
    mod = importlib.import_module(".".join((_PKG, "vcs", "content_ids")))
    repo = _make_repo(tmp_path)
    assert mod.content_ids(str(repo), "no-such-branch", "main") == ("", "")


def test_self_improve_imports_cleanly():
    importlib.import_module(".".join((_PKG, "cli", "self_improve")))


def test_lane_helpers_has_no_cache_stats_aggregator():
    mod = importlib.import_module(".".join((_PKG, "dispatch", "lane_helpers")))
    assert not hasattr(mod, "aggregate_cache_stats")
    assert "aggregate_cache_stats" not in getattr(mod, "__all__", [])


def test_no_package_module_names_a_retired_path():
    offenders = []
    for py in sorted((REPO_ROOT / "mini_ork").rglob("*.py")):
        if any(part in SKIP_DIRS for part in py.parts):
            continue
        text = py.read_text(errors="replace")
        for needle in FORBIDDEN_STRINGS:
            if needle in text:
                offenders.append(f"{py.relative_to(REPO_ROOT)}: {needle!r}")
    assert not offenders, f"retired-path references remain under mini_ork/: {offenders}"


def test_settings_catalogue_has_no_auto_merge_entry():
    mod = importlib.import_module(".".join((_PKG, "settings_catalogue")))
    names = {setting.name for setting in mod.ENGINE_SETTINGS}
    assert "MO_AUTO_MERGE" not in names
    assert "MO_OPEN_PR" not in names
