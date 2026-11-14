"""Tests for ``mini_ork.verify.touched_files`` — the shared change surface.

Every scoped verifier intersects its scan with this list, so a path this module
reports wrongly is a path no gate ever looks at. Hermetic: throwaway git repos
under ``tmp_path``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mini_ork.verify import touched_files as tf

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture()
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    # Pinned: `scoped_base` falls back to `merge-base HEAD main`, and the
    # host's default-branch setting would otherwise decide the test.
    assert _git(root, "init", "-q", "-b", "main").returncode == 0
    assert _git(root, "config", "user.email", "t@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Test").returncode == 0
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    assert _git(root, "add", ".").returncode == 0
    assert _git(root, "commit", "-qm", "seed").returncode == 0
    return root


@requires_git
def test_untracked_paths_are_reported_verbatim(repo):
    """A space or a non-ASCII byte must not hide a file from every gate."""
    (repo / "plain.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "odd name ü.py").write_text("x = 1\n", encoding="utf-8")

    surface = tf.touched_files(str(repo))

    assert "plain.py" in surface
    assert "odd name ü.py" in surface


@requires_git
def test_rename_reports_the_destination(repo):
    (repo / "old.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "add old").returncode == 0
    assert _git(repo, "mv", "old.py", "new.py").returncode == 0

    surface = tf.touched_files(str(repo))

    assert "new.py" in surface
    assert "old.py" not in surface


@requires_git
def test_modified_tracked_file_is_reported(repo):
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")

    assert "seed.txt" in tf.touched_files(str(repo))


@requires_git
def test_committed_work_on_top_of_a_base_is_reported(repo, monkeypatch):
    """Work the run already committed is part of its surface, not out of scope."""
    (repo / "added.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "add").returncode == 0

    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "HEAD~1")
    assert "added.py" in tf.touched_files(str(repo))


@requires_git
def test_noise_prefixes_are_excluded(repo):
    (repo / ".mini-ork").mkdir()
    (repo / ".mini-ork" / "run.json").write_text("{}\n", encoding="utf-8")
    (repo / "kept.py").write_text("x = 1\n", encoding="utf-8")

    surface = tf.touched_files(str(repo))

    assert "kept.py" in surface
    assert not any(path.startswith(".mini-ork") for path in surface)


def test_not_a_git_worktree(tmp_path):
    assert tf.is_git_worktree(str(tmp_path)) is False


@requires_git
def test_committed_paths_are_reported_verbatim(repo, monkeypatch):
    """``diff --name-only`` quotes too; a committed unicode file must survive."""
    (repo / "committed ü.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "add").returncode == 0

    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "HEAD~1")
    assert "committed ü.py" in tf.touched_files(str(repo))


@requires_git
def test_unresolvable_base_ref_falls_through(repo, monkeypatch):
    """A base that does not resolve is worse than none: it hides the base.

    Returning the bogus ref made every diff against it fail, so the surface was
    only the dirty tree — committed work disappeared from every gate.
    """
    (repo / "committed.py").write_text("x = 1\n", encoding="utf-8")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-qm", "add").returncode == 0

    monkeypatch.setenv("MO_GOAL_SCOPED_BASE", "deadbeefdeadbeef")
    base = tf.scoped_base(str(repo))
    # The bogus ref is discarded, not passed on: a caller that diffs against it
    # gets no diff at all, and every pre-existing import looks newly added.
    assert base and base != "deadbeefdeadbeef"
    assert _git(repo, "rev-parse", "--verify", base).returncode == 0


@requires_git
def test_subdirectory_root_reports_root_relative_paths(repo):
    """``git -C sub`` still speaks repo-root paths; the gate must translate.

    Joining ``pkg/sub/a.py`` onto a root of ``pkg/sub`` stats a file that does
    not exist, so every gate rooted at a subdirectory scanned nothing while
    reporting a clean pass.
    """
    sub = repo / "pkg" / "sub"
    sub.mkdir(parents=True)
    (sub / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "outside.py").write_text("x = 1\n", encoding="utf-8")

    surface = tf.touched_files(str(sub))

    assert "a.py" in surface
    assert "outside.py" not in surface
    assert tf.repo_prefix(str(sub)) == "pkg/sub"
