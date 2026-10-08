"""`clean` still releases a worktree's claims when git no longer knows the worktree.

Another process can prune a worktree's admin dir (`.git/worktrees/<slug>`)
while its directory stays on disk. `git worktree remove` then fails, and
`clean` used to stop there, leaving the slug's `--owns` claims live, so every
later worktree on the same files was refused.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKTREE_PY = REPO / "scripts" / "mini_ork_worktree.py"
GIT_IDENTITY = ("-c", "user.name=mo-test", "-c", "user.email=mo-test@example.invalid")

# Same lane push-guard stripping as tests/unit/test_merge_oss_guard.py.
ENV_BASE = {k: v for k, v in os.environ.items()
            if k != "GIT_CONFIG_COUNT"
            and not k.startswith("GIT_CONFIG_KEY_")
            and not k.startswith("GIT_CONFIG_VALUE_")}


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_IDENTITY, *args], cwd=cwd, env=ENV_BASE,
                          capture_output=True, text=True, check=True, timeout=60)


@pytest.fixture()
def repo(tmp_path: Path) -> dict:
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    worktrees = tmp_path / "worktrees"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True, timeout=60)
    subprocess.run(["git", "clone", str(origin), str(clone)],
                   capture_output=True, check=True, timeout=60)
    (clone / "seed.txt").write_text("seed\n")
    git("add", "seed.txt", cwd=clone)
    git("commit", "-m", "seed", cwd=clone)
    git("push", "origin", "HEAD:main", cwd=clone)
    env = {**ENV_BASE, "MINI_ORK_ROOT": str(clone), "MINI_ORK_WORKTREES_DIR": str(worktrees),
           "MINI_ORK_OWNERSHIP_FILE": str(worktrees / ".ownership"), "MO_CONCORD": "0",
           "MINI_ORK_HOME": str(tmp_path / "home")}
    return {"clone": clone, "worktrees": worktrees, "env": env}


def run_wt(repo: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(WORKTREE_PY), *args], cwd=repo["clone"],
                          env=repo["env"], capture_output=True, text=True, timeout=120)


def test_clean_releases_claims_of_a_pruned_worktree(repo: dict) -> None:
    assert run_wt(repo, "create", "alpha", "--owns", "src/app.py").returncode == 0
    assert "src/app.py" in run_wt(repo, "owners").stdout
    # Another process prunes the admin dir; the worktree directory survives.
    shutil.rmtree(repo["clone"] / ".git" / "worktrees" / "alpha")

    cleaned = run_wt(repo, "clean", "alpha")
    assert cleaned.returncode == 0, cleaned.stderr
    assert "not a registered worktree" in cleaned.stderr
    assert "src/app.py" not in run_wt(repo, "owners").stdout
    # A new worktree may now claim the same file.
    assert run_wt(repo, "create", "beta", "--owns", "src/app.py").returncode == 0
