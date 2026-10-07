"""framework-edit static-check: `diff-apply-check-clean` against the repo root.

The implementer edits the target tree in place and framework-edit.diff is cut
from it, so a forward `git apply --check` against that same tree always failed
('already exists in working directory'): 46/46 framework-edit runs on
2026-10-07 recorded this false failure. The check now passes when the diff
applies forward OR in reverse, and still fails for a diff the tree contradicts.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "static-check.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _repo(tmp: Path) -> Path:
    root = tmp / "target"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "mod.py").write_text("VALUE = 1\n")
    _git(root, "add", "mod.py")
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
