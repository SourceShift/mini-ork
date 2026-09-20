"""The deploy-target resync in the goal-loop worker-restart binding.

Regression: the jina figure-preservation fix was merged to main, but the branch
the book worker actually ran was 14 commits behind, so every chapter kept losing
its figures while the fix was reported as shipped. The restart binding is the
"make the code live" edge, so it must carry the product branch forward — without
ever dropping a commit or blocking the deploy on a conflict.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_BINDING = (
    Path(__file__).resolve().parents[2]
    / "kickoffs"
    / "book-goal-loop"
    / "binding"
    / "restart_worker.py"
)


def _load_binding():
    spec = importlib.util.spec_from_file_location("goal_loop_restart_worker", _BINDING)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def binding():
    return _load_binding()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def deploy_target(tmp_path: Path) -> Path:
    """A repo whose `deploy` branch has diverged from `main` by one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "shared.ts").write_text("base\n")
    _commit(repo, "one")
    _git(repo, "checkout", "-q", "-b", "deploy")
    (repo / "loop-fix.ts").write_text("child fix\n")
    _commit(repo, "the loop's own fix")
    _git(repo, "checkout", "-q", "main")
    (repo / "product.ts").write_text("product fix\n")
    _commit(repo, "the product branch advances")
    _git(repo, "checkout", "-q", "deploy")
    return repo


def test_merges_the_product_branch_forward(binding, deploy_target):
    why = binding._sync_upstream(str(deploy_target))

    assert why.startswith("merged main"), why
    # The product branch is now an ancestor of the deploy branch.
    subprocess.run(
        ["git", "-C", str(deploy_target), "merge-base", "--is-ancestor", "main", "HEAD"],
        check=True,
    )
    assert binding._sync_upstream(str(deploy_target)).startswith("already contains main")


def test_never_drops_the_loops_own_commit(binding, deploy_target):
    binding._sync_upstream(str(deploy_target))
    subjects = _git(deploy_target, "log", "--format=%s", "main..HEAD")

    assert "the loop's own fix" in subjects


def test_a_conflict_leaves_the_tree_untouched(binding, deploy_target, capsys):
    # main and deploy both edit the same line: an unresolvable conflict.
    (deploy_target / "shared.ts").write_text("deploy side\n")
    _commit(deploy_target, "deploy edits shared")
    head_before = _git(deploy_target, "rev-parse", "HEAD")
    _git(deploy_target, "checkout", "-q", "main")
    (deploy_target / "shared.ts").write_text("main side\n")
    _commit(deploy_target, "main edits shared")
    _git(deploy_target, "checkout", "-q", "deploy")

    why = binding._sync_upstream(str(deploy_target))

    assert why.startswith("merge conflict against main"), why
    assert "shared.ts" in why
    # No merge left in progress, and HEAD is exactly where we started.
    assert not (deploy_target / ".git" / "MERGE_HEAD").exists()
    assert _git(deploy_target, "rev-parse", "HEAD") == head_before
    assert "resolve by hand" in capsys.readouterr().err


def test_opt_out_is_a_no_op(binding, deploy_target, monkeypatch):
    monkeypatch.setenv("MO_GOAL_SYNC_UPSTREAM", "0")

    assert binding._sync_upstream(str(deploy_target)) == "disabled (MO_GOAL_SYNC_UPSTREAM=0)"
    assert _git(deploy_target, "log", "--format=%s", "main..HEAD") == "the loop's own fix"
