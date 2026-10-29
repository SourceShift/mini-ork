"""``.githooks/pre-push`` must not let its children write to the pushing repo.

git exports ``GIT_DIR`` to hooks (in a linked worktree it is always set), and
every layer of the hook — including the review lenses, which are agents that
may run this suite — inherits it. A test that runs ``git init`` / ``config`` /
``add -A`` / ``commit`` in a temp dir then operates on the pushing repository:
on 2026-10-29 one push left 5 fixture commits on the branch, ``[user] name =
t`` in the shared ``.git/config``, and ``core.bare = true`` (from a no-path
``git init --bare``), which broke every work-tree command in the primary.

Each test builds its own primary + linked worktree under ``tmp_path`` and runs
a child — standing in for anything the hook spawns — with ``GIT_DIR`` exported
only into the child's environment. The control proves the harness can see the
corruption; the second test proves ``_drop_repo_env`` prevents it.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".githooks" / "pre-push"

# What a hook's grandchild does when a test it runs builds fixture repos: the
# same calls tests/test_recovery_closure.py and the bare-remote tests make.
_CHILD = r"""
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile
hook, scrub, scratch_root = sys.argv[1], sys.argv[2] == "1", sys.argv[3]
if scrub:
    loader = importlib.machinery.SourceFileLoader("pre_push_child", hook)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod._drop_repo_env()

def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True)

repo = tempfile.mkdtemp(dir=scratch_root)
git("-C", repo, "init", "-q")
git("-C", repo, "config", "user.name", "t")
git("-C", repo, "config", "user.email", "t@t")
with open(os.path.join(repo, "seed.txt"), "w") as fh:
    fh.write("seed\n")
git("-C", repo, "add", "-A")
git("-C", repo, "commit", "-qm", "base")
git("init", "-q", "--bare", cwd=tempfile.mkdtemp(dir=scratch_root))
"""


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _primary_with_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    """A primary checkout with one commit, and a linked worktree on ``feat``."""
    primary, wt = tmp_path / "primary", tmp_path / "wt"
    primary.mkdir()
    ident = ("-c", "user.email=o@example.invalid", "-c", "user.name=owner")
    assert _git("init", "-q", cwd=primary).returncode == 0
    (primary / "kept.txt").write_text("real content\n")
    _git("add", "kept.txt", cwd=primary)
    assert _git(*ident, "commit", "-qm", "real", cwd=primary).returncode == 0
    assert _git("worktree", "add", "-q", str(wt), "-b", "feat", cwd=primary).returncode == 0
    gitdir = _git("rev-parse", "--absolute-git-dir", cwd=wt).stdout.strip()
    return primary, wt, gitdir


def _run_hook_child(wt: Path, gitdir: str, tmp_path: Path, *, scrub: bool) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_DIR"] = gitdir  # what git hands a hook running in a linked worktree
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    subprocess.run(
        [sys.executable, "-c", _CHILD, str(HOOK), "1" if scrub else "0", str(scratch)],
        cwd=wt, env=env, check=True, capture_output=True,
    )


def _damage(primary: Path, wt: Path, tip: str) -> dict[str, object]:
    return {
        "branch_moved": _git("rev-parse", "HEAD", cwd=wt).stdout.strip() != tip,
        "user_name": _git("config", "--local", "--get", "user.name", cwd=primary).stdout.strip(),
        "bare": _git("config", "--local", "--get", "core.bare", cwd=primary).stdout.strip() == "true",
        "primary_usable": _git("status", "--porcelain", cwd=primary).returncode == 0,
    }


def _load_hook():
    loader = importlib.machinery.SourceFileLoader("pre_push_under_test", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_control_an_inherited_git_dir_corrupts_the_pushing_repo(tmp_path: Path) -> None:
    """Without the scrub the harness must observe damage, or the next test proves nothing."""
    primary, wt, gitdir = _primary_with_worktree(tmp_path)
    tip = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()

    _run_hook_child(wt, gitdir, tmp_path, scrub=False)

    damage = _damage(primary, wt, tip)
    assert (damage["branch_moved"] or damage["user_name"] == "t" or damage["bare"]), damage


def test_drop_repo_env_keeps_hook_children_out_of_the_pushing_repo(tmp_path: Path) -> None:
    primary, wt, gitdir = _primary_with_worktree(tmp_path)
    tip = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()

    _run_hook_child(wt, gitdir, tmp_path, scrub=True)

    assert _damage(primary, wt, tip) == {
        "branch_moved": False, "user_name": "", "bare": False, "primary_usable": True,
    }
    assert (wt / ".git").is_file(), "the worktree is still a linked worktree"


def test_drop_repo_env_removes_every_repo_binding_variable(monkeypatch) -> None:
    hook = _load_hook()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_PREFIX"):
        monkeypatch.setenv(name, "/nowhere")
    monkeypatch.setenv("GIT_EDITOR", "true")  # not repo-binding: must survive

    hook._drop_repo_env()

    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_PREFIX"):
        assert name not in os.environ, name
    assert os.environ["GIT_EDITOR"] == "true"
