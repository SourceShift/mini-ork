"""Unit tests for ``_salvage_before_revert`` (rollback salvage).

Operator rule: a failed run must never throw the agent's work away. Before
either revert path destroys the working tree, ``_salvage_before_revert``
snapshots the run's change set into ``refs/mini-ork/salvage/<run_id>`` (a
commit parented on HEAD, built in a TEMP index so the real index, HEAD and
every branch stay untouched) plus a restorable ``salvage.patch``.

Mirrors ``test_revert_inplace_diff_py.py``: in-place temp git repo, no shared
fixture, ``monkeypatch.setenv`` for the target-cwd and keep-worktree env flags.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex
from mini_ork.cli import execute_handlers as exh


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


def _mk_repo(tmp_path: Path) -> Path:
    """A repo with ``a.txt``, ``b.txt`` and ``keep.txt`` committed at base."""
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a-original\n")
    (repo / "b.txt").write_text("b-original\n")
    (repo / "keep.txt").write_text("keep-original\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _mk_repo_no_identity(tmp_path: Path) -> Path:
    """Same repo, but with NO local ``user.name``/``user.email`` — the base
    commit is made with one-shot ``-c`` identity so DoD #6 actually exercises
    the salvage's explicit-identity override."""
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a-original\n")
    (repo / "b.txt").write_text("b-original\n")
    (repo / "keep.txt").write_text("keep-original\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=x", "-c", "user.email=x", "commit", "-qm", "base")
    return repo


def _run_state(repo: Path, tmp_path: Path) -> Path:
    """The run's change set: ``a.txt`` modified, ``new.txt`` created,
    ``b.txt`` deleted, plus an unrelated dirty ``keep.txt`` the run did NOT
    list. The run dir's ``implementer-summary.json`` records the run's paths."""
    (repo / "a.txt").write_text("a-modified\n")
    (repo / "new.txt").write_text("new-content\n")
    (repo / "b.txt").unlink()
    (repo / "keep.txt").write_text("keep-dirty\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"files_changed": ["a.txt", "new.txt", "b.txt"]}))
    return run_dir


def _dispatch_rollback(tmp_path: Path, repo: Path, run_dir: Path, strategy: str) -> tuple:
    wf = tmp_path / "workflow.yaml"
    wf.write_text(f"rollback_strategy: {strategy}\n")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}))
    return ex.dispatch_node(
        ("rb1", "rollback", "undo", "", "serial", "", "rollback", ""),
        root=str(tmp_path), run_dir=str(run_dir), plan_path=str(plan),
        task_class="code_fix", db="", run_id="r",
        dispatch_fn=lambda *a: (0, ""), recipe="framework-edit",
        workflow=str(wf))


def _clear_rollback_env(monkeypatch) -> None:
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_ROLLBACK_KEEP_WORKTREE", raising=False)


# ── DoD 1: the snapshot ref carries exactly the run's delta ─────────────────


def test_salvage_creates_ref_with_only_the_runs_delta(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    run_dir = _run_state(repo, tmp_path)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    res = exh._salvage_before_revert(str(repo), str(run_dir), "r")

    assert res is not None
    assert res["saved"] is True
    assert res["ref"] == "refs/mini-ork/salvage/r"
    assert res["files"] == ["a.txt", "b.txt", "new.txt"]
    sha = _git(repo, "rev-parse", "refs/mini-ork/salvage/r").stdout.strip()
    assert _git(repo, "show", f"{sha}:a.txt").stdout == "a-modified\n"
    assert _git(repo, "show", f"{sha}:new.txt").stdout == "new-content\n"
    assert _git(repo, "cat-file", "-e", f"{sha}:b.txt").returncode != 0
    # Unrelated dirt is not the run's work: keep.txt stays at HEAD's version.
    assert _git(repo, "show", f"{sha}:keep.txt").stdout == "keep-original\n"


# ── DoD 2: nothing outside the ref is touched ───────────────────────────────


def test_salvage_leaves_index_head_and_worktree_untouched(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    run_dir = _run_state(repo, tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    exh._salvage_before_revert(str(repo), str(run_dir), "r")

    assert _git(repo, "diff", "--cached", "--quiet").returncode == 0
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (repo / "a.txt").read_text() == "a-modified\n"
    assert (repo / "new.txt").read_text() == "new-content\n"
    assert not (repo / "b.txt").exists()
    assert (repo / "keep.txt").read_text() == "keep-dirty\n"


# ── DoD 3: a non-empty, applyable patch ─────────────────────────────────────


def test_salvage_writes_an_applyable_patch(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    run_dir = _run_state(repo, tmp_path)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    exh._salvage_before_revert(str(repo), str(run_dir), "r")

    assert (run_dir / "salvage.json").is_file()
    assert (run_dir / "salvage.patch").is_file()
    assert (run_dir / "salvage.patch").stat().st_size > 0
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    assert _git(clone, "apply", str(run_dir / "salvage.patch")).returncode == 0
    assert (clone / "a.txt").read_text() == "a-modified\n"
    assert (clone / "new.txt").read_text() == "new-content\n"
    assert not (clone / "b.txt").exists()
    assert (clone / "keep.txt").read_text() == "keep-original\n"


def test_new_untracked_files_are_captured_but_preexisting_dirt_is_not(
        tmp_path, monkeypatch):
    """The third capture source: files untracked NOW that were not untracked at
    run start. A pre-existing untracked scratch file must stay out of the
    snapshot, while the run's newly created untracked file is captured."""
    repo = _mk_repo(tmp_path)
    (repo / "pre.txt").write_text("pre-existing scratch\n")
    (repo / "run-created.txt").write_text("created by the run\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pre-implementer-untracked").write_text("pre.txt\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    res = exh._salvage_before_revert(str(repo), str(run_dir), "r")

    assert res is not None
    assert res["saved"] is True
    assert "run-created.txt" in res["files"]
    assert "pre.txt" not in res["files"]
    sha = _git(repo, "rev-parse", "refs/mini-ork/salvage/r").stdout.strip()
    assert _git(repo, "show", f"{sha}:run-created.txt").stdout == "created by the run\n"
    assert _git(repo, "cat-file", "-e", f"{sha}:pre.txt").returncode != 0


# ── DoD 4: through _handle_rollback, the work is restorable afterwards ───────


def test_rollback_handler_salvages_then_reverts_and_restores(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    run_dir = _run_state(repo, tmp_path)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    _clear_rollback_env(monkeypatch)

    rc, fr = _dispatch_rollback(tmp_path, repo, run_dir, "revert_branch")

    assert (rc, fr) == (0, "done")  # rollback never re-fails the run
    assert (repo / "a.txt").read_text() == "a-original\n"
    assert (repo / "b.txt").read_text() == "b-original\n"
    assert not (repo / "new.txt").exists()
    assert (repo / "keep.txt").read_text() == "keep-dirty\n"  # never touched
    sha = _git(repo, "rev-parse", "refs/mini-ork/salvage/r").stdout.strip()
    _git(repo, "checkout", sha, "--", "a.txt", "new.txt")
    assert (repo / "a.txt").read_text() == "a-modified\n"
    assert (repo / "new.txt").read_text() == "new-content\n"


# ── DoD 5: when nothing can be saved, do NOT revert ──────────────────────────


def test_failed_salvage_skips_revert_and_warns(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    run_dir = _run_state(repo, tmp_path)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    _clear_rollback_env(monkeypatch)

    real_run_git = exh._run_git

    def _fail_git(args, cwd, *, env=None):
        if "commit-tree" in args or (args and args[0] == "diff"):
            return subprocess.CompletedProcess(args, 1, b"", b"mocked failure")
        return real_run_git(args, cwd, env=env)

    monkeypatch.setattr(exh, "_run_git", _fail_git)

    rc, fr = _dispatch_rollback(tmp_path, repo, run_dir, "revert_branch")

    assert (rc, fr) == (0, "done")
    assert (repo / "a.txt").read_text() == "a-modified\n"  # run's work intact
    assert (repo / "new.txt").read_text() == "new-content\n"
    assert (repo / "keep.txt").read_text() == "keep-dirty\n"
    assert "work could not be saved" in capsys.readouterr().err


# ── DoD 6: works with no git identity (CI runners) ──────────────────────────


def test_salvage_works_without_git_identity(tmp_path, monkeypatch):
    repo = _mk_repo_no_identity(tmp_path)
    run_dir = _run_state(repo, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n    useConfigOnly = true\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    res = exh._salvage_before_revert(str(repo), str(run_dir), "r")

    assert res is not None
    assert res["saved"] is True
    assert res["sha"]  # the explicit -c identity beat useConfigOnly
    assert _git(repo, "rev-parse", "refs/mini-ork/salvage/r").returncode == 0


# ── DoD 7: no paths means no ref and the normal revert ───────────────────────


def test_no_paths_means_no_ref_and_normal_revert(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    (repo / "a.txt").write_text("a-broken\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"files_changed": []}))
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    _clear_rollback_env(monkeypatch)

    rc, fr = _dispatch_rollback(tmp_path, repo, run_dir, "revert_branch")

    assert (rc, fr) == (0, "done")
    assert _git(repo, "rev-parse", "refs/mini-ork/salvage/r").returncode != 0
    # "no files_changed recorded" leaves the working tree untouched.
    assert (repo / "a.txt").read_text() == "a-broken\n"


# ── DoD 8: absolute + repo-relative duplicates collapse to one path ──────────


def test_absolute_path_and_review_diff_dedupe_to_one(tmp_path, monkeypatch):
    """The same file recorded once by absolute path (``files_changed``) and once
    repo-relative (``review-diff.patch`` header) must appear exactly once in
    ``salvage.json["files"]`` and the printed restore command."""
    repo = _mk_repo(tmp_path)
    (repo / "a.txt").write_text("a-modified\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"files_changed": [str(repo / "a.txt")]}))
    (run_dir / "review-diff.patch").write_text(
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    res = exh._salvage_before_revert(str(repo), str(run_dir), "r")

    assert res is not None
    assert res["saved"] is True
    assert res["files"] == ["a.txt"]
    doc = json.loads((run_dir / "salvage.json").read_text())
    assert doc["files"] == ["a.txt"]
    checkout = [c for c in doc["restore"] if c.startswith("git checkout ")][0]
    assert checkout == f"git checkout {doc['sha']} -- a.txt"
    assert checkout.count("a.txt") == 1


# ── keep_worktree: nothing is destroyed, so nothing is salvaged ──────────────


def test_keep_worktree_skips_salvage(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    run_dir = _run_state(repo, tmp_path)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.setenv("MINI_ORK_ROLLBACK_KEEP_WORKTREE", "1")
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    rc, fr = _dispatch_rollback(tmp_path, repo, run_dir, "revert_branch")

    assert (rc, fr) == (0, "done")
    assert _git(repo, "rev-parse", "refs/mini-ork/salvage/r").returncode != 0
    assert (repo / "a.txt").read_text() == "a-modified\n"  # not reverted


# ── both call sites: the in-place strategy salvages too ─────────────────────


def test_inplace_strategy_salvages_before_revert(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    (repo / "a.txt").write_text("a-broken\n")
    (repo / "made.py").write_text("new\n")
    _git(repo, "add", "made.py")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(_git(repo, "diff", "HEAD").stdout)
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"files_changed": ["a.txt", "made.py"]}))
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    _clear_rollback_env(monkeypatch)

    rc, fr = _dispatch_rollback(
        tmp_path, repo, run_dir, "keep_run_artifacts_discard_worktree")

    assert (rc, fr) == (0, "done")
    assert (repo / "a.txt").read_text() == "a-original\n"
    assert not (repo / "made.py").exists()
    sha = _git(repo, "rev-parse", "refs/mini-ork/salvage/r").stdout.strip()
    assert _git(repo, "show", f"{sha}:a.txt").stdout == "a-broken\n"
    assert _git(repo, "show", f"{sha}:made.py").stdout == "new\n"
