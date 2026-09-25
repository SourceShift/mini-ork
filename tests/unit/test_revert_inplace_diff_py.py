"""Unit tests for the in-place rollback compensation
(``keep_run_artifacts_discard_worktree``).

framework-edit and self-migrate both declare this strategy in their
``workflow.yaml``, and it was implemented nowhere. A failed run therefore left
the harvested diff sitting in the target tree — a preflight against
run-1788363267-21773-se1 found 4 staged paths behind — so the next serial epic
started from a dirty base and its reviewer diff swept in the dead run's work.

The in-place agent may leave any mix of states, so all four are pinned here:
staged edits, unstaged edits, a created file, and a path outside the diff.
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
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.py").write_text("original\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def _run_with_diff(tmp_path: Path, repo: Path) -> Path:
    """A run dir carrying the run's own harvested diff (what rollback reverses)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(_git(repo, "diff", "HEAD").stdout)
    return run_dir


def _dirty(repo: Path) -> str:
    return _git(repo, "status", "--porcelain").stdout.strip()


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


# ── the four states the agent leaves behind ─────────────────────────────────


def test_staged_edit_is_reverted(tmp_path, monkeypatch):
    """The agent `git add`s its work; ``git apply -R`` alone would leave the
    index entry behind (the 3 index corpses from run-1788363267-21773-se1)."""
    repo = _mk_repo(tmp_path)
    (repo / "tracked.py").write_text("broken\n")
    run_dir = _run_with_diff(tmp_path, repo)
    _git(repo, "add", "tracked.py")
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    assert exh._revert_inplace_diff(str(run_dir), str(tmp_path)) is True
    assert (repo / "tracked.py").read_text() == "original\n"
    assert _dirty(repo) == ""  # worktree AND index


def test_unstaged_edit_is_reverted(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    (repo / "tracked.py").write_text("broken\n")
    run_dir = _run_with_diff(tmp_path, repo)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    assert exh._revert_inplace_diff(str(run_dir), str(tmp_path)) is True
    assert (repo / "tracked.py").read_text() == "original\n"
    assert _dirty(repo) == ""


def test_created_file_is_removed_and_unstaged(tmp_path, monkeypatch):
    """A file absent from HEAD has no content to restore — it must be unstaged
    and unlinked, not merely emptied."""
    repo = _mk_repo(tmp_path)
    (repo / "made.py").write_text("new\n")
    _git(repo, "add", "made.py")
    run_dir = _run_with_diff(tmp_path, repo)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    assert exh._revert_inplace_diff(str(run_dir), str(tmp_path)) is True
    assert not (repo / "made.py").exists()
    assert _dirty(repo) == ""


def test_paths_outside_the_diff_are_never_touched(tmp_path, monkeypatch):
    """A concurrent session's dirty file is not this run's to revert."""
    repo = _mk_repo(tmp_path)
    (repo / "tracked.py").write_text("broken\n")
    run_dir = _run_with_diff(tmp_path, repo)
    (repo / "other.py").write_text("another session's work\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    assert exh._revert_inplace_diff(str(run_dir), str(tmp_path)) is True
    assert (repo / "tracked.py").read_text() == "original\n"
    assert (repo / "other.py").read_text() == "another session's work\n"


def test_a_diff_cannot_reach_outside_the_target_repo(tmp_path, monkeypatch):
    """Assert the invariant, not the guard: whatever git does with a traversing
    path, the file outside MO_TARGET_CWD keeps its bytes."""
    repo = _mk_repo(tmp_path)
    outside = tmp_path / "evil.txt"
    outside.write_text("do not touch\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "framework-edit.diff").write_text(
        "diff --git a/../evil.txt b/../evil.txt\n"
        "--- a/../evil.txt\n"
        "+++ b/../evil.txt\n"
        "@@ -1 +1 @@\n"
        "-do not touch\n"
        "+owned\n")
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    exh._revert_inplace_diff(str(run_dir), str(tmp_path))

    assert outside.read_text() == "do not touch\n"


# ── no-ops and reporting ────────────────────────────────────────────────────


def test_no_diff_artifact_is_a_noop(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    (repo / "tracked.py").write_text("broken\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    assert exh._revert_inplace_diff(str(run_dir), str(tmp_path)) is True
    assert (repo / "tracked.py").read_text() == "broken\n"
    assert "no framework-edit.diff" in capsys.readouterr().err


# ── handler wiring ──────────────────────────────────────────────────────────


def test_rollback_handler_discards_the_inplace_diff(tmp_path, monkeypatch):
    """End-to-end: the declared strategy now actually reverts the target tree."""
    repo = _mk_repo(tmp_path)
    (repo / "tracked.py").write_text("broken\n")
    run_dir = _run_with_diff(tmp_path, repo)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_ROLLBACK_KEEP_WORKTREE", raising=False)

    rc, fr = _dispatch_rollback(
        tmp_path, repo, run_dir, "keep_run_artifacts_discard_worktree")

    assert (rc, fr) == (0, "done")  # rollback never re-fails the run
    assert (repo / "tracked.py").read_text() == "original\n"
    # "keep_run_ARTIFACTS": the run's own evidence survives the revert.
    assert (run_dir / "framework-edit.diff").is_file()


def test_rollback_handler_keeps_the_inplace_edit_when_an_outer_loop_owns_verify(
        tmp_path, monkeypatch):
    """MINI_ORK_ROLLBACK_KEEP_WORKTREE must cover BOTH strategies.

    In a closed RSI loop (goal-loop) the authoritative gate is downstream —
    deploy -> regen -> DB flip — and it tests the FILE state the implementer
    produced. An in-sandbox revert would destroy that edit before the real gate
    ever sees it, so the outer-loop escape hatch has to apply to the in-place
    strategy too, not only to revert_branch.
    """
    repo = _mk_repo(tmp_path)
    (repo / "tracked.py").write_text("fix by implementer\n")
    run_dir = _run_with_diff(tmp_path, repo)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.setenv("MINI_ORK_ROLLBACK_KEEP_WORKTREE", "1")
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    rc, fr = _dispatch_rollback(
        tmp_path, repo, run_dir, "keep_run_artifacts_discard_worktree")

    assert (rc, fr) == (0, "done")
    assert (repo / "tracked.py").read_text() == "fix by implementer\n"
