"""Unit tests: .githooks/* Python ports (bash-removal Phase 4).

Drives each hook as a subprocess with fixture stdin/env:

  reference-transaction
    (a) normal commit update on refs/heads/wt/* (old != zero)      → pass
    (b) zero→new feature branch on refs/heads/*                    → blocked
    (c) same with ALLOW_WORKTREE_BRANCH_CREATE=1                   → pass
    (d) grafted foreign commit (synthetic two-root repo)           → rejected
    (e) same with MO_ALLOW_FOREIGN_REF=1                           → pass

  pre-push
    (f) MO_README_DRIFT_SKIP=1 → exit 0, bypass message, no L1 call
    (g) otherwise runs L1 (run_layer1 invoked with sys.executable argv)

  post-commit
    (h) MO_REVERSION_GUARD_DISABLED=1 → exit 0, no log
    (i) watchdog restores a deleted file and writes the TSV log line
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".githooks"
ZERO = "0" * 40

REF_TX = HOOKS / "reference-transaction"
PRE_PUSH = HOOKS / "pre-push"
POST_COMMIT = HOOKS / "post-commit"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_file(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run_hook(hook: Path, argv: list[str], stdin: str, repo: Path,
              env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(hook), *argv],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
        env=env,
    )


def _call_main(mod, stdin_text: str, monkeypatch) -> int:
    """Invoke an imported hook's main() with fixture stdin, restoring cwd."""
    cwd = os.getcwd()
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    try:
        return mod.main()
    finally:
        os.chdir(cwd)


def _load_module(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ─────────────────────────────────────────────────────────────────────────────
# reference-transaction
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def tx_repo(tmp_path: Path) -> Path:
    """Repo with two commits on main; HEAD on main (anchor = HEAD)."""
    repo = _init_repo(tmp_path / "tx")
    _commit_file(repo, "a.txt", "one\n", "c1")
    _commit_file(repo, "a.txt", "two\n", "c2")
    return repo


def test_ref_tx_allows_normal_commit_update_on_wt_branch(tx_repo: Path):
    old = _git(tx_repo, "rev-parse", "HEAD~1").stdout.strip()
    new = _git(tx_repo, "rev-parse", "HEAD").stdout.strip()
    r = _run_hook(REF_TX, ["prepared"], f"{old} {new} refs/heads/wt/task-x\n", tx_repo)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""


def test_ref_tx_non_prepared_state_passes(tx_repo: Path):
    sha = _git(tx_repo, "rev-parse", "HEAD").stdout.strip()
    r = _run_hook(REF_TX, ["committed"], f"{ZERO} {sha} refs/heads/feat\n", tx_repo)
    assert r.returncode == 0


def test_ref_tx_blocks_direct_feature_branch_creation(tx_repo: Path):
    sha = _git(tx_repo, "rev-parse", "HEAD").stdout.strip()
    r = _run_hook(REF_TX, ["prepared"], f"{ZERO} {sha} refs/heads/feat-x\n", tx_repo)
    assert r.returncode == 1
    assert "[worktree-guard] REJECT direct feature-branch creation: feat-x" in r.stderr
    assert "scripts/mini-ork-worktree.sh create <task-slug>" in r.stderr
    assert "ALLOW_WORKTREE_BRANCH_CREATE=1" in r.stderr


def test_ref_tx_allows_branch_create_with_escape_env(tx_repo: Path):
    sha = _git(tx_repo, "rev-parse", "HEAD").stdout.strip()
    r = _run_hook(
        REF_TX, ["prepared"], f"{ZERO} {sha} refs/heads/feat-x\n", tx_repo,
        env_extra={"ALLOW_WORKTREE_BRANCH_CREATE": "1"},
    )
    assert r.returncode == 0, r.stderr


def test_ref_tx_allows_main_branch_creation(tx_repo: Path):
    # refs/heads/main / refs/heads/master are exempt from the worktree guard.
    sha = _git(tx_repo, "rev-parse", "HEAD").stdout.strip()
    r = _run_hook(REF_TX, ["prepared"], f"{ZERO} {sha} refs/heads/master\n", tx_repo)
    assert r.returncode == 0, r.stderr


def test_ref_tx_allows_branch_deletion(tx_repo: Path):
    sha = _git(tx_repo, "rev-parse", "HEAD").stdout.strip()
    r = _run_hook(REF_TX, ["prepared"], f"{sha} {ZERO} refs/heads/feat-x\n", tx_repo)
    assert r.returncode == 0, r.stderr


def test_ref_tx_rejects_foreign_commit(tx_repo: Path):
    # Synthetic two-root repo: orphan branch with unrelated history.
    main_sha = _git(tx_repo, "rev-parse", "HEAD").stdout.strip()
    _git(tx_repo, "checkout", "-q", "--orphan", "foreign-root")
    _git(tx_repo, "rm", "-q", "-rf", ".")
    foreign = _commit_file(tx_repo, "other.txt", "unrelated\n", "foreign c1")
    _git(tx_repo, "checkout", "-q", "main")  # anchor must resolve to main history
    assert _git(tx_repo, "rev-parse", "HEAD").stdout.strip() == main_sha
    assert _git(tx_repo, "merge-base", foreign, main_sha).stdout.strip() == ""

    r = _run_hook(
        REF_TX, ["prepared"], f"{ZERO} {foreign} refs/codex/curated-sync\n", tx_repo,
        env_extra={"ALLOW_WORKTREE_BRANCH_CREATE": "1"},
    )
    assert r.returncode == 1
    assert f"[mini-ork ref-guard] REJECT refs/codex/curated-sync -> {foreign[:12]}" in r.stderr
    assert "FOREIGN to this repo" in r.stderr
    assert "MO_ALLOW_FOREIGN_REF=1" in r.stderr


def test_ref_tx_foreign_allowed_with_escape_env(tx_repo: Path):
    _git(tx_repo, "checkout", "-q", "--orphan", "foreign-root")
    _git(tx_repo, "rm", "-q", "-rf", ".")
    foreign = _commit_file(tx_repo, "other.txt", "unrelated\n", "foreign c1")
    _git(tx_repo, "checkout", "-q", "main")

    r = _run_hook(
        REF_TX, ["prepared"], f"{ZERO} {foreign} refs/codex/curated-sync\n", tx_repo,
        env_extra={"ALLOW_WORKTREE_BRANCH_CREATE": "1", "MO_ALLOW_FOREIGN_REF": "1"},
    )
    assert r.returncode == 0, r.stderr


def test_ref_tx_ignores_tags_and_remotes(tx_repo: Path):
    _git(tx_repo, "checkout", "-q", "--orphan", "foreign-root")
    _git(tx_repo, "rm", "-q", "-rf", ".")
    foreign = _commit_file(tx_repo, "other.txt", "unrelated\n", "foreign c1")
    _git(tx_repo, "checkout", "-q", "main")
    # Foreign commits on non-HEAD/branch/codex refs are not the vector.
    stdin = f"{ZERO} {foreign} refs/tags/v9\n{ZERO} {foreign} refs/remotes/origin/x\n"
    r = _run_hook(REF_TX, ["prepared"], stdin, tx_repo)
    assert r.returncode == 0, r.stderr


# ─────────────────────────────────────────────────────────────────────────────
# pre-push
# ─────────────────────────────────────────────────────────────────────────────
def test_pre_push_master_bypass_skips_everything(tmp_path: Path):
    repo = _init_repo(tmp_path / "pp")
    _commit_file(repo, "a.txt", "one\n", "c1")
    r = _run_hook(
        PRE_PUSH, [], "refs/heads/main abc refs/heads/main def\n", repo,
        env_extra={"MO_README_DRIFT_SKIP": "1"},
    )
    assert r.returncode == 0
    assert "pre-push: MO_README_DRIFT_SKIP=1 — bypassing all drift checks" in r.stdout
    assert "Layer 1" not in r.stdout


def test_pre_push_runs_layer1_otherwise(monkeypatch, tmp_path: Path):
    mod = _load_module(PRE_PUSH, "githooks_pre_push")
    calls = []

    def fake_layer1(repo_root: Path) -> int:
        calls.append(repo_root)
        return 0

    monkeypatch.setattr(mod, "run_layer1", fake_layer1)
    monkeypatch.setenv("MO_REVIEW_SKIP", "1")  # keep Layer 3 (bash lib) out
    monkeypatch.delenv("MO_README_DRIFT_SKIP", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    rc = _call_main(mod, "refs/heads/feat abc refs/heads/feat def\n", monkeypatch)
    assert rc == 0
    assert calls == [mod.REPO_ROOT]

    # Real argv shape: sys.executable + scripts/readme_claim_check.py.
    argv = [sys.executable, str(mod.REPO_ROOT / "scripts" / "readme_claim_check.py")]
    assert argv[0] == sys.executable
    assert argv[1].endswith("scripts/readme_claim_check.py")


def test_pre_push_layer1_failure_blocks_non_tag(monkeypatch, capsys):
    mod = _load_module(PRE_PUSH, "githooks_pre_push_l1fail")
    monkeypatch.setattr(mod, "run_layer1", lambda repo_root: 1)
    monkeypatch.setenv("MO_REVIEW_SKIP", "1")
    monkeypatch.delenv("MO_README_DRIFT_SKIP", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    rc = _call_main(mod, "refs/heads/feat abc refs/heads/feat def\n", monkeypatch)
    assert rc == 1
    out = capsys.readouterr().out
    assert "✗ pre-push: Layer 1 found mechanical drift — push BLOCKED." in out


def test_pre_push_layer1_failure_advisory_on_tag(monkeypatch, capsys):
    mod = _load_module(PRE_PUSH, "githooks_pre_push_l1tag")
    monkeypatch.setattr(mod, "run_layer1", lambda repo_root: 1)
    monkeypatch.setenv("MO_REVIEW_SKIP", "1")
    monkeypatch.delenv("MO_README_DRIFT_SKIP", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    rc = _call_main(mod, "refs/tags/v1 abc refs/tags/v1 def\n", monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "advisory — tag push proceeds." in out
    assert "not pushing to main → skipping L2 panel." in out


# ─────────────────────────────────────────────────────────────────────────────
# post-commit
# ─────────────────────────────────────────────────────────────────────────────
def test_post_commit_disabled_env_exits_clean(tmp_path: Path):
    repo = _init_repo(tmp_path / "pc")
    _commit_file(repo, "a.txt", "one\n", "c1")
    r = _run_hook(POST_COMMIT, [], "", repo,
                  env_extra={"MO_REVERSION_GUARD_DISABLED": "1"})
    assert r.returncode == 0
    assert not (repo / ".mini-ork" / "file-reversion-guard.log").exists()


def test_post_commit_watchdog_restores_deleted_file(tmp_path: Path):
    repo = _init_repo(tmp_path / "pc2")
    _commit_file(repo, "seed.txt", "seed\n", "seed")
    sha = _commit_file(repo, "a.txt", "one\n", "c1")
    r = _run_hook(
        POST_COMMIT, [], "", repo,
        env_extra={"MO_REVERSION_GUARD_WATCH_S": "4", "MO_REVERSION_GUARD_POLL_S": "1"},
    )
    assert r.returncode == 0

    # Racing process deletes the just-committed file; watchdog must restore it.
    (repo / "a.txt").unlink()
    log = repo / ".mini-ork" / "file-reversion-guard.log"
    deadline = time.time() + 15
    line = ""
    while time.time() < deadline:
        if log.exists():
            lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
            if lines:
                line = lines[-1]
                break
        time.sleep(0.5)
    assert line, "watchdog wrote no recovery line"
    ts, log_sha, log_file, action = line.split("\t")
    assert log_sha == sha
    assert log_file == "a.txt"
    assert action == f"restored-deleted-from-{sha}"
    assert ts.endswith("Z") and "T" in ts
    assert (repo / "a.txt").read_text() == "one\n"
    # Wait for the detached watchdog to finish so it can't race later tests.
    time.sleep(5)


def test_post_commit_watchdog_restores_reverted_file(tmp_path: Path):
    repo = _init_repo(tmp_path / "pc3")
    _commit_file(repo, "a.txt", "v1\n", "c1")
    parent = _git(repo, "rev-parse", "HEAD").stdout.strip()
    sha = _commit_file(repo, "a.txt", "v2\n", "c2")
    r = _run_hook(
        POST_COMMIT, [], "", repo,
        env_extra={"MO_REVERSION_GUARD_WATCH_S": "4", "MO_REVERSION_GUARD_POLL_S": "1"},
    )
    assert r.returncode == 0

    # Racing process reverts the file to its HEAD~1 content.
    (repo / "a.txt").write_text("v1\n")
    log = repo / ".mini-ork" / "file-reversion-guard.log"
    deadline = time.time() + 15
    line = ""
    while time.time() < deadline:
        if log.exists():
            lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
            if lines:
                line = lines[-1]
                break
        time.sleep(0.5)
    assert line, "watchdog wrote no recovery line"
    _, log_sha, log_file, action = line.split("\t")
    assert log_sha == sha
    assert log_file == "a.txt"
    assert action == f"restored-reverted-to-{parent}"
    assert (repo / "a.txt").read_text() == "v2\n"
    time.sleep(5)
