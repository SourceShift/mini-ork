"""Regression: the reviewer diff must actually contain the implementer's edit.

``files_changed`` is recorded ABSOLUTE (the publisher's commit gate and
``_revert_branch`` both need that shape), but git accepts a pathspec only
relative to the repo root. Passing the absolute list straight through matched
NOTHING and exited 0, so ``review-diff.patch`` was 0 bytes, the reviewer was told
"(no diff)" and passed a run whose edit it never saw. Observed live: a goal-loop
code-fix child reported pass while the researcher worktree stayed at its pre-run
commit.

Two properties are pinned here: a real edit is visible regardless of how the
declared paths are shaped, and a declared-but-absent change is recorded as a
no-op instead of being silently reviewable as ambient state.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "B.txt").write_text("B original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")


def _summary(run_dir: Path, worktree: Path, files: list[str]) -> None:
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "status": "implemented",
        "worktree_path": str(worktree),
        "files_changed": files,
    }))


# ── _review_pathspecs ───────────────────────────────────────────────────────


def test_pathspecs_relativize_absolute_paths_inside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert ex._review_pathspecs(str(repo), [str(repo / "B.txt")]) == ["B.txt"]


def test_pathspecs_drop_entries_that_resolve_outside_the_repo(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    outside = tmp_path / "elsewhere" / "B.txt"
    outside.parent.mkdir()
    outside.write_text("not ours\n")
    assert ex._review_pathspecs(str(repo), [str(outside)]) == []
    # A '..' escape is dropped whether spelled relatively or absolutely.
    assert ex._review_pathspecs(str(repo), ["../elsewhere/B.txt"]) == []


def test_pathspecs_drop_git_pathspec_magic(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert ex._review_pathspecs(str(repo), [":(top)B.txt", ":(exclude)B.txt"]) == []


def test_pathspecs_keep_a_symlinked_entry_whose_own_path_is_inside(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    target = tmp_path / "primary" / "generated"
    target.mkdir(parents=True)
    (repo / "generated").symlink_to(target)
    # The entry names a path inside the tree even though its target is not.
    assert ex._review_pathspecs(str(repo), [str(repo / "generated")]) == ["generated"]


def test_pathspecs_dedupe_and_ignore_non_entries(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert ex._review_pathspecs(str(repo), [str(repo / "B.txt"), str(repo / "B.txt"), ""]) == ["B.txt"]


# ── _assemble_reviewer_inputs ───────────────────────────────────────────────


def test_absolute_files_changed_still_produce_a_real_diff(tmp_path, monkeypatch):
    """The exact live failure: absolute declared paths must NOT yield 0 bytes."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_baseline(str(run_dir))
    (repo / "B.txt").write_text("B IMPLEMENTER EDIT\n")
    _summary(run_dir, repo, [str(repo / "B.txt")])

    ex._assemble_reviewer_inputs(str(run_dir))
    diff = (run_dir / "review-diff.patch").read_text()
    assert "B IMPLEMENTER EDIT" in diff
    assert (run_dir / "review-diff-noop.json").exists() is False


def test_outside_entry_does_not_hide_the_in_repo_edit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    outside = tmp_path / "primary" / "baml_client"
    outside.mkdir(parents=True)

    ex._capture_pre_impl_baseline(str(run_dir))
    (repo / "B.txt").write_text("B IMPLEMENTER EDIT\n")
    _summary(run_dir, repo, [outside.as_posix(), str(repo / "B.txt")])

    ex._assemble_reviewer_inputs(str(run_dir))
    assert "B IMPLEMENTER EDIT" in (run_dir / "review-diff.patch").read_text()


def test_only_escaping_entries_widen_to_the_full_diff(tmp_path, monkeypatch):
    """Never narrow to nothing: an unusable pathspec list captures the whole delta."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    outside = tmp_path / "primary" / "baml_client"
    outside.mkdir(parents=True)

    ex._capture_pre_impl_baseline(str(run_dir))
    (repo / "B.txt").write_text("B IMPLEMENTER EDIT\n")
    _summary(run_dir, repo, [outside.as_posix()])

    ex._assemble_reviewer_inputs(str(run_dir))
    assert "B IMPLEMENTER EDIT" in (run_dir / "review-diff.patch").read_text()
    # A genuine delta exists, so this is not a no-op even though no pathspec survived.
    assert (run_dir / "review-diff-noop.json").exists() is False


def test_declared_change_that_does_not_exist_is_recorded_as_no_op(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_baseline(str(run_dir))
    # The implementer claims a file it never touched; the tree is unchanged.
    _summary(run_dir, repo, [str(repo / "never-touched.txt")])

    block = ex._assemble_reviewer_inputs(str(run_dir))
    marker = json.loads((run_dir / "review-diff-noop.json").read_text())
    assert marker["status"] == "no_op"
    assert marker["declared_files"] == [str(repo / "never-touched.txt")]
    assert "no diff" in block
    assert "real failure, never a pass" in block


def test_untracked_new_file_is_not_a_no_op(tmp_path, monkeypatch):
    """A created file has no tracked diff; it is still real work, not a no-op."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_baseline(str(run_dir))
    (repo / "brand-new.txt").write_text("new work\n")
    _summary(run_dir, repo, [str(repo / "brand-new.txt")])

    ex._assemble_reviewer_inputs(str(run_dir))
    assert (run_dir / "review-diff-noop.json").exists() is False


def test_no_summary_leaves_the_empty_diff_branch_untouched(tmp_path):
    """No implementer summary → nothing was declared → never a no-op marker."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    block = ex._assemble_reviewer_inputs(str(run_dir))
    assert "(no diff)" in block
    assert (run_dir / "review-diff-noop.json").exists() is False
