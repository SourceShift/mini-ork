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


def _strip_git_identity(monkeypatch) -> None:
    """Make the ambient environment look like a fresh runner: no usable identity.

    This file's own commits pass ``GIT_*`` env, but the binding's git calls
    inherit the process env. A developer machine supplies an identity from
    ``~/.gitconfig`` or, on macOS, from the login record, so the bug below is
    invisible locally and appears only in CI — which is how the deploy resync
    stayed broken. Emptying the four variables and pointing both config files at
    the null device leaves git with no name to write into the merge commit,
    which is the state the runner is in.
    """
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


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


def test_a_merge_refused_before_it_starts_is_not_called_a_conflict(
        binding, deploy_target, capsys):
    """Only a merge that stops on content is a conflict. This one never starts:
    uncommitted local changes the merge would overwrite make git refuse outright,
    leaving no conflicted paths. That shape used to be reported as ``merge
    conflict against main`` too, which is how a missing git identity (see the
    test below) stayed invisible while the deploy target kept falling behind.
    """
    (deploy_target / "shared.ts").write_text("deploy side\n")
    _commit(deploy_target, "deploy edits shared")
    _git(deploy_target, "checkout", "-q", "main")
    (deploy_target / "shared.ts").write_text("main side\n")
    _commit(deploy_target, "main edits shared")
    _git(deploy_target, "checkout", "-q", "deploy")
    # Uncommitted, and on the file the merge would have to rewrite.
    (deploy_target / "shared.ts").write_text("uncommitted local edit\n")
    head_before = _git(deploy_target, "rev-parse", "HEAD")

    why = binding._sync_upstream(str(deploy_target))

    assert why.startswith("merge failed against main"), why
    assert "conflict against" not in why
    assert not (deploy_target / ".git" / "MERGE_HEAD").exists()
    assert _git(deploy_target, "rev-parse", "HEAD") == head_before
    # The uncommitted work is left exactly as it was, not clobbered by the abort.
    assert (deploy_target / "shared.ts").read_text() == "uncommitted local edit\n"
    assert "not a content conflict" in capsys.readouterr().err


def test_opt_out_is_a_no_op(binding, deploy_target, monkeypatch):
    monkeypatch.setenv("MO_GOAL_SYNC_UPSTREAM", "0")

    assert binding._sync_upstream(str(deploy_target)) == "disabled (MO_GOAL_SYNC_UPSTREAM=0)"
    assert _git(deploy_target, "log", "--format=%s", "main..HEAD") == "the loop's own fix"


def test_merges_in_an_environment_with_no_git_identity(
        binding, deploy_target, monkeypatch):
    """A merge writes a commit, so an identity-less environment kills it with
    'empty ident name ... not allowed'. The binding used to report that as
    ``merge conflict against main``, so the deploy target silently stopped
    carrying the product branch forward — the regression this file exists to
    catch. It has to supply its own fallback identity instead.
    """
    _strip_git_identity(monkeypatch)

    why = binding._sync_upstream(str(deploy_target))

    assert why.startswith("merged main"), why
    # The fallback is the project-wide one, matching vcs/rebase_guard.py and
    # vcs/auto_merge.py rather than an ad-hoc name.
    assert _git(deploy_target, "log", "-1", "--format=%an") == "mini-ork"
