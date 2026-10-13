"""framework-edit static-check: `diff-apply-check-clean` against the repo root.

The implementer edits the target tree in place and framework-edit.diff is cut
from it, so a forward `git apply --check` against that same tree always failed
('already exists in working directory'): 46/46 framework-edit runs on
2026-10-07 recorded this false failure. The check now passes when the diff
diff applies forward OR in reverse, and still fails for a diff the tree contradicts.

The `python-lint-blocking-tier` block below exercises the new ruff-over-the-
diff check: skip when no `.py` changed or the patched copy's pyproject.toml
lacks a [tool.ruff.lint] table, pass on a clean edit, fail on an F401 violation,
and fail when the resolver cannot find ruff on a host that has it installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "static-check.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _repo(tmp: Path, *, pyproject: str = "") -> Path:
    """Create a minimal git repo with a `mod.py` and optional `pyproject.toml`.

    The default (no pyproject) matches the original harness used by the
    diff-apply-check-clean tests. Lint tests pass the pyproject content they
    need the patched copy to carry so the resolver/has-table check sees it."""
    root = tmp / "target"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "mod.py").write_text("VALUE = 1\n")
    files = ["mod.py"]
    if pyproject:
        (root / "pyproject.toml").write_text(pyproject)
        files.append("pyproject.toml")
    _git(root, "add", *files)
    _git(root, "commit", "-q", "-m", "base")
    return root


def _diff_for(root: Path, new_text: str) -> str:
    (root / "mod.py").write_text(new_text)
    diff = _git(root, "diff")
    _git(root, "checkout", "-q", "--", "mod.py")
    return diff


def _row(tmp: Path, root: Path, diff: str) -> str:
    run_dir = tmp / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(diff)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env.update(MINI_ORK_RUN_DIR=str(run_dir), MINI_ORK_ROOT=str(root))
    subprocess.run([sys.executable, str(VERIFIER)], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    rows = (run_dir / "verifier-static-check.checks.tsv").read_text().splitlines()
    return next(r.split("\t")[2] for r in rows if r.startswith("diff-apply-check-clean\t"))


def test_diff_not_yet_applied_passes(tmp_path) -> None:
    root = _repo(tmp_path)
    assert _row(tmp_path, root, _diff_for(root, "VALUE = 2\n")) == "true"


def test_diff_already_applied_in_place_passes(tmp_path) -> None:
    # The framework-edit default: the tree already carries the edit.
    root = _repo(tmp_path)
    diff = _diff_for(root, "VALUE = 2\n")
    (root / "mod.py").write_text("VALUE = 2\n")
    assert _row(tmp_path, root, diff) == "true"


def test_diff_the_tree_contradicts_fails(tmp_path) -> None:
    root = _repo(tmp_path)
    diff = _diff_for(root, "VALUE = 2\n")
    (root / "mod.py").write_text("VALUE = 3\n")  # neither the base nor the diff's result
    assert _row(tmp_path, root, diff) == "false"


# ── python-lint-blocking-tier ───────────────────────────────────────────────
# The check has three exit shapes: skip (no .py changed / no [tool.ruff.lint]
# table → returns True with a `skip: …` line in the evidence log), pass
# (ruff exits 0 over the changed .py files), fail (ruff finds a blocking-tier
# violation or the resolver cannot find ruff). The wrapper helper below runs the
# verifier in a subprocess so it sees a clean module each call; one variant
# monkey-patches shutil.which and importlib.util.find_spec BEFORE the verifier
# imports so the resolver returns None even on a host that has ruff installed.

_HAVE_RUFF = shutil.which("ruff") is not None

_NO_RUFF_WRAPPER = """\
import shutil
import importlib.util
import sys


_orig_which = shutil.which
def _patched_which(name, *args, **kwargs):
    if name == "ruff":
        return None
    return _orig_which(name, *args, **kwargs)
shutil.which = _patched_which


_orig_find_spec = importlib.util.find_spec
def _patched_find_spec(name, *args, **kwargs):
    if name == "ruff":
        return None
    return _orig_find_spec(name, *args, **kwargs)
importlib.util.find_spec = _patched_find_spec


import runpy
runpy.run_path({verifier!r}, run_name="__main__")
"""


def _lint_row(tmp: Path, root: Path, diff: str, *, force_no_ruff: bool = False) -> str:
    run_dir = tmp / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(diff)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MINI_ORK_", "MO_"))}
    env.update(MINI_ORK_RUN_DIR=str(run_dir), MINI_ORK_ROOT=str(root))
    if force_no_ruff:
        wrapper = tmp / "no_ruff_wrapper.py"
        wrapper.write_text(_NO_RUFF_WRAPPER.format(verifier=str(VERIFIER)))
        cmd = [sys.executable, str(wrapper)]
    else:
        cmd = [sys.executable, str(VERIFIER)]
    subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=120)
    rows = (run_dir / "verifier-static-check.checks.tsv").read_text().splitlines()
    return next(r.split("\t")[2] for r in rows if r.startswith("python-lint-blocking-tier\t"))


def _evidence(tmp: Path) -> str:
    return (tmp / "run" / "verifier-static-check.log").read_text()


def test_lint_skips_when_no_python_files_changed(tmp_path) -> None:
    """A non-Python file changed → no .py to lint, check skips, row passes."""
    root = _repo(tmp_path)
    # Add a non-Python file so the diff is non-empty but contains no .py change.
    (root / "notes.txt").write_text("hi\n")
    _git(root, "add", "notes.txt")
    _git(root, "commit", "-q", "-m", "notes")
    (root / "notes.txt").write_text("bye\n")
    diff = _git(root, "diff")
    _git(root, "checkout", "-q", "--", "notes.txt")
    assert _lint_row(tmp_path, root, diff) == "true"
    assert "skip: no Python files changed" in _evidence(tmp_path)


def test_lint_skips_when_no_ruff_lint_table(tmp_path) -> None:
    """pyproject.toml exists but has no [tool.ruff.lint] → check skips, row passes.

    Skipping here is a deliberate choice: the patched copy's repo defines what
    'blocking tier' means, and an unrelated pyproject has no project-specific
    blocking tier. Inventing one would let a contributor's W391 (blank line at
    end of file) silently fail a release."""
    root = _repo(tmp_path, pyproject='[tool.other]\nkey = "value"\n')
    diff = _diff_for(root, "VALUE = 2\n")
    assert _lint_row(tmp_path, root, diff) == "true"
    assert "skip: pyproject.toml has no [tool.ruff.lint] table" in _evidence(tmp_path)


@pytest.mark.skipif(not _HAVE_RUFF, reason="ruff not installed")
def test_lint_passes_clean_python(tmp_path) -> None:
    """Clean Python + [tool.ruff.lint] select=["F"] → ruff blocking tier green."""
    root = _repo(tmp_path, pyproject='[tool.ruff.lint]\nselect = ["F"]\n')
    diff = _diff_for(root, "VALUE = 2\n")
    assert _lint_row(tmp_path, root, diff) == "true"


@pytest.mark.skipif(not _HAVE_RUFF, reason="ruff not installed")
def test_lint_fails_with_blocking_violation(tmp_path) -> None:
    """F401 (unused import) → ruff blocking tier fails."""
    root = _repo(tmp_path, pyproject='[tool.ruff.lint]\nselect = ["F"]\n')
    diff = _diff_for(root, "import os\nVALUE = 2\n")
    assert _lint_row(tmp_path, root, diff) == "false"


def test_lint_fails_when_ruff_unavailable(tmp_path) -> None:
    """Resolver sees no ruff → check fails with the install-message line.

    The wrapper monkey-patches shutil.which and importlib.util.find_spec
    BEFORE the verifier imports, so the resolver returns None even on a host
    where ruff is installed — the check must fail closed rather than silently
    pass when the linter is missing."""
    root = _repo(tmp_path, pyproject='[tool.ruff.lint]\nselect = ["F"]\n')
    diff = _diff_for(root, "VALUE = 2\n")
    assert _lint_row(tmp_path, root, diff, force_no_ruff=True) == "false"
    log = _evidence(tmp_path)
    assert "ruff not found" in log
    assert 'pip install "ruff>=0.6"' in log