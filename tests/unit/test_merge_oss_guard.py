"""OSS-guard contract tests for `scripts/mini_ork_worktree.py merge`.

`main` is public: a merge must refuse a branch that carries unclaimed paths,
confidential terms or credential-shaped secrets, and push nothing when it
refuses. Every test drives the real CLI against a throwaway git topology (bare
origin + clone + worktree) with the green gate stubbed via
`MINI_ORK_TEST_CMD=true`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKTREE_PY = REPO / "scripts" / "mini_ork_worktree.py"

GIT_IDENTITY = ("-c", "user.name=mo-test", "-c", "user.email=mo-test@example.invalid")

# Stub the green gate: these tests exercise the OSS guard, not the tests.
GREEN = {"MINI_ORK_TEST_CMD": "true"}


def _lane_env() -> dict:
    """``os.environ`` minus the harness's lane-level push guard.

    An agent lane disables pushes by exporting a global ``GIT_CONFIG_COUNT`` /
    ``GIT_CONFIG_KEY_0=remote.origin.pushurl=no-push://…``. That guard protects
    the real checkout; these tests push only to a throwaway temp origin, so it
    is stripped from the git calls and CLI runs they spawn.
    """
    return {k: v for k, v in os.environ.items()
            if k != "GIT_CONFIG_COUNT"
            and not k.startswith("GIT_CONFIG_KEY_")
            and not k.startswith("GIT_CONFIG_VALUE_")}


ENV_BASE = _lane_env()


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=cwd, env=ENV_BASE, capture_output=True, text=True, check=check, timeout=60,
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
        **ENV_BASE,
        "MINI_ORK_ROOT": str(clone),
        "MINI_ORK_WORKTREES_DIR": str(worktrees),
        "MINI_ORK_OWNERSHIP_FILE": str(worktrees / ".ownership"),
        "MO_CONCORD": "0",
        "GIT_AUTHOR_NAME": "mo-test", "GIT_AUTHOR_EMAIL": "mo-test@example.invalid",
        "GIT_COMMITTER_NAME": "mo-test", "GIT_COMMITTER_EMAIL": "mo-test@example.invalid",
        # Point the private terms file at an (absent) sandbox path so a real
        # one on the developer's machine can never leak into a test.
        "MINI_ORK_HOME": str(tmp_path / "home"),
    }
    return {"origin": origin, "clone": clone, "worktrees": worktrees,
            "home": tmp_path / "home", "env": env}


def run_wt(repo: dict, *args: str,
           extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**repo["env"], **(extra_env or {})}
    return subprocess.run(
        [sys.executable, str(WORKTREE_PY), *args],
        cwd=repo["clone"], env=env, capture_output=True, text=True, timeout=120,
    )


def origin_main(repo: dict) -> str:
    return git("rev-parse", "refs/heads/main", cwd=repo["origin"]).stdout.strip()


def commit_file(wt: Path, relpath: str, content: str) -> str:
    path = wt / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git("add", relpath, cwd=wt)
    git("commit", "-m", f"add {relpath}", cwd=wt)
    return git("rev-parse", "HEAD", cwd=wt).stdout.strip()


def test_merge_pushes_when_the_branch_stays_inside_its_claims(repo: dict) -> None:
    assert run_wt(repo, "create", "alpha", "--owns", "src/app.py").returncode == 0
    wt = repo["worktrees"] / "alpha"
    commit_file(wt, "src/app.py", "print('ok')\n")
    # A worktree's own kickoff is always allowed, even though it is unclaimed.
    head = commit_file(wt, "kickoffs/auto/alpha.md", "# alpha\n")

    merged = run_wt(repo, "merge", "alpha", extra_env=GREEN)
    assert merged.returncode == 0, merged.stderr
    assert "merged wt/alpha -> origin/main" in merged.stdout
    assert origin_main(repo) == head


def test_merge_refuses_an_unclaimed_path_and_pushes_nothing(repo: dict) -> None:
    assert run_wt(repo, "create", "beta", "--owns", "src/app.py").returncode == 0
    wt = repo["worktrees"] / "beta"
    commit_file(wt, "src/app.py", "print('ok')\n")
    commit_file(wt, "n8n-reply.txt", "draft reply\n")
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "beta", extra_env=GREEN)
    assert refused.returncode == 1
    assert "merge refused" in refused.stderr
    assert "outside this worktree's claims" in refused.stderr
    assert "n8n-reply.txt" in refused.stderr
    assert origin_main(repo) == before


def test_merge_refuses_confidential_terms_and_names_only_the_term(repo: dict) -> None:
    assert run_wt(repo, "create", "gamma", "--owns", "docs/notes.md").returncode == 0
    wt = repo["worktrees"] / "gamma"
    commit_file(wt, "docs/notes.md", "notes\nwe are fundraising for our company\n")
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "gamma", extra_env=GREEN)
    assert refused.returncode == 1
    assert "docs/notes.md:2" in refused.stderr
    assert "fundrais" in refused.stderr
    # Only file:line and the matched term are shown — never the whole line.
    assert "fundraising" not in refused.stderr
    assert "our company" not in refused.stderr
    assert origin_main(repo) == before


def test_merge_refuses_a_real_shaped_key_but_allows_a_fixture(repo: dict) -> None:
    assert run_wt(repo, "create", "delta", "--owns", "config/settings.py").returncode == 0
    wt = repo["worktrees"] / "delta"
    commit_file(wt, "config/settings.py", 'KEY = "sk-live-abcdefghijklmnopqrstuvwx"\n')
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "delta", extra_env=GREEN)
    assert refused.returncode == 1
    assert "config/settings.py:1" in refused.stderr
    # The secret itself is never echoed.
    assert "sk-live-abcdefghijklmnopqrstuvwx" not in refused.stderr
    assert origin_main(repo) == before

    assert run_wt(repo, "create", "epsilon", "--owns", "config/fixtures.py").returncode == 0
    wt2 = repo["worktrees"] / "epsilon"
    head = commit_file(wt2, "config/fixtures.py",
                       'KEY = "sk-test-value-never-shown-abcdefgh"\n')
    allowed = run_wt(repo, "merge", "epsilon", extra_env=GREEN)
    assert allowed.returncode == 0, allowed.stderr
    assert origin_main(repo) == head


def test_merge_refuses_a_private_term_from_the_terms_file(repo: dict) -> None:
    repo["home"].mkdir()
    (repo["home"] / "oss-guard-terms.txt").write_text("acmecorp\n")
    assert run_wt(repo, "create", "zeta", "--owns", "docs/notes.md").returncode == 0
    wt = repo["worktrees"] / "zeta"
    commit_file(wt, "docs/notes.md", "our partner AcmeCorp will announce\n")
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "zeta", extra_env=GREEN)
    assert refused.returncode == 1
    assert "merge refused" in refused.stderr
    assert "docs/notes.md:1" in refused.stderr
    assert origin_main(repo) == before


def test_merge_allow_unclaimed_override_proceeds_with_a_warning(repo: dict) -> None:
    assert run_wt(repo, "create", "eta", "--owns", "src/app.py").returncode == 0
    wt = repo["worktrees"] / "eta"
    commit_file(wt, "src/app.py", "print('ok')\n")
    head = commit_file(wt, "rogue.txt", "extra\n")

    merged = run_wt(repo, "merge", "eta",
                    extra_env={**GREEN, "MO_MERGE_ALLOW_UNCLAIMED": "1"})
    assert merged.returncode == 0, merged.stderr
    assert "MO_MERGE_ALLOW_UNCLAIMED" in merged.stderr
    assert origin_main(repo) == head


def test_merge_allow_oss_override_proceeds_with_a_warning(repo: dict) -> None:
    assert run_wt(repo, "create", "theta", "--owns", "docs/notes.md").returncode == 0
    wt = repo["worktrees"] / "theta"
    head = commit_file(wt, "docs/notes.md", "we are fundraising for our company\n")

    merged = run_wt(repo, "merge", "theta",
                    extra_env={**GREEN, "MO_MERGE_ALLOW_OSS": "1"})
    assert merged.returncode == 0, merged.stderr
    assert "MO_MERGE_ALLOW_OSS" in merged.stderr
    assert origin_main(repo) == head


def test_merge_refuses_a_secret_added_then_removed_in_a_later_commit(repo: dict) -> None:
    # The net diff is clean, but the first commit still carries the key into
    # public history once pushed.
    assert run_wt(repo, "create", "iota", "--owns", "config/settings.py").returncode == 0
    wt = repo["worktrees"] / "iota"
    commit_file(wt, "config/settings.py", 'KEY = "sk-live-abcdefghijklmnopqrstuvwx"\n')
    commit_file(wt, "config/settings.py", 'KEY = None\n')
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "iota", extra_env=GREEN)
    assert refused.returncode == 1
    assert "config/settings.py:1 matches openai_key" in refused.stderr
    assert origin_main(repo) == before


def test_merge_refuses_a_term_in_a_commit_message(repo: dict) -> None:
    assert run_wt(repo, "create", "kappa", "--owns", "src/app.py").returncode == 0
    wt = repo["worktrees"] / "kappa"
    (wt / "src").mkdir()
    (wt / "src" / "app.py").write_text("print('ok')\n")
    git("add", "src/app.py", cwd=wt)
    git("commit", "-m", "app\n\nready for the investor demo", cwd=wt)
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "kappa", extra_env=GREEN)
    assert refused.returncode == 1
    assert "message:3 matches investor" in refused.stderr
    assert origin_main(repo) == before


def test_merge_scans_an_added_line_that_starts_with_plus_plus(repo: dict) -> None:
    assert run_wt(repo, "create", "lambda", "--owns", "src/loop.c").returncode == 0
    wt = repo["worktrees"] / "lambda"
    commit_file(wt, "src/loop.c", 'int i;\n++i; char *k = "sk-live-abcdefghijklmnopqrstuvwx";\n')
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "lambda", extra_env=GREEN)
    assert refused.returncode == 1
    assert "src/loop.c:2 matches openai_key" in refused.stderr
    assert origin_main(repo) == before


def test_merge_fails_closed_under_coloured_and_prefixless_diff_config(repo: dict) -> None:
    git("config", "color.ui", "always", cwd=repo["clone"])
    git("config", "diff.noprefix", "true", cwd=repo["clone"])
    assert run_wt(repo, "create", "mu", "--owns", "docs/notes.md").returncode == 0
    wt = repo["worktrees"] / "mu"
    commit_file(wt, "docs/notes.md", "the seed round closes soon\n")
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "mu", extra_env=GREEN)
    assert refused.returncode == 1
    assert "docs/notes.md:1 matches seed round" in refused.stderr
    assert origin_main(repo) == before


def test_merge_refuses_a_rename_out_of_an_unclaimed_path(repo: dict) -> None:
    assert run_wt(repo, "create", "nu", "--owns", "src/app.py").returncode == 0
    wt = repo["worktrees"] / "nu"
    (wt / "src").mkdir()
    git("mv", "seed.txt", "src/app.py", cwd=wt)
    git("commit", "-m", "move seed", cwd=wt)
    before = origin_main(repo)

    refused = run_wt(repo, "merge", "nu", extra_env=GREEN)
    assert refused.returncode == 1
    assert "seed.txt" in refused.stderr
    assert origin_main(repo) == before


def test_a_private_term_is_named_only_by_its_number(repo: dict) -> None:
    repo["home"].mkdir()
    (repo["home"] / "oss-guard-terms.txt").write_text("\nacmecorp\n")
    assert run_wt(repo, "create", "xi", "--owns", "docs/notes.md").returncode == 0
    wt = repo["worktrees"] / "xi"
    commit_file(wt, "docs/notes.md", "our partner AcmeCorp will announce\n")

    refused = run_wt(repo, "merge", "xi", extra_env=GREEN)
    assert refused.returncode == 1
    assert "docs/notes.md:1 matches private term #2" in refused.stderr
    assert "acmecorp" not in refused.stderr.lower()
