"""Subprocess contract tests for scripts/mini_ork_worktree.py + readme_claim_check.py.

Runs the ported worktree CLI against a throwaway git topology (bare origin +
clone) with MINI_ORK_WORKTREES_DIR pointed at a tmp dir, so the real dev
worktree registry is never touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKTREE_PY = REPO / "scripts" / "mini_ork_worktree.py"
CLAIM_CHECK_PY = REPO / "scripts" / "readme_claim_check.py"

GIT_IDENTITY = ("-c", "user.name=mo-test", "-c", "user.email=mo-test@example.invalid")


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=cwd, capture_output=True, text=True, check=check, timeout=60,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> dict:
    """Bare origin + main checkout clone + isolated worktrees dir."""
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
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(clone),
        "MINI_ORK_WORKTREES_DIR": str(worktrees),
        "MINI_ORK_OWNERSHIP_FILE": str(worktrees / ".ownership"),
    }
    return {"origin": origin, "clone": clone, "worktrees": worktrees, "env": env}


def run_wt(repo: dict, *args: str, extra_env: dict | None = None,
           cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {**repo["env"], **(extra_env or {})}
    return subprocess.run(
        [sys.executable, str(WORKTREE_PY), *args],
        cwd=cwd or repo["clone"], env=env, capture_output=True, text=True, timeout=120,
    )


def ownership_rows(repo: dict) -> list[tuple[str, str]]:
    path = Path(repo["env"]["MINI_ORK_OWNERSHIP_FILE"])
    if not path.exists():
        return []
    return [tuple(line.rstrip("\n").split("\t")[:2])
            for line in path.read_text().splitlines() if line]


def commit_in(wt: Path, name: str) -> str:
    (wt / name).write_text(f"{name}\n")
    git("add", name, cwd=wt)
    git("commit", "-m", f"add {name}", cwd=wt)
    return git("rev-parse", "HEAD", cwd=wt).stdout.strip()


def test_claim_create_refuse_overlap_release_round_trip(repo: dict) -> None:
    ok = run_wt(repo, "create", "alpha", "--owns", "mini_ork/foo.py",
                "--owns", "docs/plans")
    assert ok.returncode == 0, ok.stderr
    assert "[mo-worktree] ready:" in ok.stdout
    assert "branch wt/alpha" in ok.stdout
    assert "[mo-worktree] claimed:" in ok.stderr
    assert (repo["worktrees"] / "alpha").is_dir()
    assert git("branch", "--list", "wt/alpha", cwd=repo["clone"]).stdout.strip()
    assert ownership_rows(repo) == [("alpha", "mini_ork/foo.py"),
                                    ("alpha", "docs/plans")]

    # Path-prefix overlap with a live claim is refused.
    conflict = run_wt(repo, "create", "beta", "--owns", "mini_ork")
    assert conflict.returncode == 1
    assert "ownership conflict" in conflict.stderr
    assert "held by live worktree 'alpha'" in conflict.stderr
    assert not (repo["worktrees"] / "beta").exists()

    # Release frees the surface; the same claim now succeeds.
    rel = run_wt(repo, "release", "alpha")
    assert rel.returncode == 0, rel.stderr
    assert "[mo-worktree] released claims for alpha" in rel.stdout
    assert ownership_rows(repo) == []
    ok2 = run_wt(repo, "create", "beta", "--owns", "mini_ork")
    assert ok2.returncode == 0, ok2.stderr
    assert ownership_rows(repo) == [("beta", "mini_ork")]


def test_merge_green_gate_honors_mini_ork_test_cmd(repo: dict) -> None:
    assert run_wt(repo, "create", "gamma").returncode == 0
    wt = repo["worktrees"] / "gamma"
    head = commit_in(wt, "gamma.txt")

    merged = run_wt(repo, "merge", "gamma", extra_env={"MINI_ORK_TEST_CMD": "true"})
    assert merged.returncode == 0, merged.stderr
    assert "merged wt/gamma -> origin/main" in merged.stdout
    origin_main = git("rev-parse", "refs/heads/main", cwd=repo["origin"]).stdout.strip()
    assert origin_main == head


def test_merge_green_gate_failure_blocks_push(repo: dict) -> None:
    assert run_wt(repo, "create", "delta").returncode == 0
    wt = repo["worktrees"] / "delta"
    commit_in(wt, "delta.txt")
    before = git("rev-parse", "refs/heads/main", cwd=repo["origin"]).stdout.strip()

    failed = run_wt(repo, "merge", "delta", extra_env={"MINI_ORK_TEST_CMD": "false"})
    assert failed.returncode == 1
    assert "green gate failed (false)" in failed.stderr
    assert "fix before merging" in failed.stderr
    after = git("rev-parse", "refs/heads/main", cwd=repo["origin"]).stdout.strip()
    assert after == before


def test_clean_removes_worktree_branch_and_claims(repo: dict) -> None:
    assert run_wt(repo, "create", "epsilon", "--owns", "lib/x.sh").returncode == 0
    wt = repo["worktrees"] / "epsilon"
    assert ownership_rows(repo) == [("epsilon", "lib/x.sh")]

    cleaned = run_wt(repo, "clean", "epsilon")
    assert cleaned.returncode == 0, cleaned.stderr
    assert "[mo-worktree] cleaned epsilon" in cleaned.stdout
    assert not wt.exists()
    assert git("branch", "--list", "wt/epsilon", cwd=repo["clone"]).stdout.strip() == ""
    assert ownership_rows(repo) == []


def test_owners_prunes_claims_of_vanished_worktrees(repo: dict) -> None:
    assert run_wt(repo, "create", "zeta", "--owns", "bin/tool").returncode == 0
    # Simulate an abandoned worktree: delete the dir behind the registry's back.
    import shutil
    shutil.rmtree(repo["worktrees"] / "zeta")
    listed = run_wt(repo, "owners")
    assert listed.returncode == 0
    assert "(no active claims)" in listed.stdout


def test_readme_claim_check_exits_zero_on_real_repo() -> None:
    result = subprocess.run(
        [sys.executable, str(CLAIM_CHECK_PY)],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLEAN" in result.stdout
