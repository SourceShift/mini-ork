"""Regression: framework-edit diff capture must not sweep up pre-existing dirt.

The implementer edits MO_TARGET_CWD in place. Before this fix, the reviewer
diff was `git diff` (working tree vs HEAD), so any unrelated uncommitted change
already present — e.g. a CONCURRENT session sharing the working tree — was
captured into review-diff.patch and could be reviewed and published as if it
were the run's own output. Observed repeatedly.

The fix snapshots the tree before the implementer runs
(``_capture_pre_impl_baseline`` → ``pre-implementer-ref`` via non-destructive
``git stash create``) and diffs against that baseline, so only the implementer's
delta survives. These tests drive the real functions against a real throwaway
git repo — no mocks.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_execute as ex  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "A.txt").write_text("A original\n")
    (repo / "B.txt").write_text("B original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")


def test_capture_excludes_preexisting_dirt(tmp_path, monkeypatch):
    """A concurrent session's uncommitted edit to A.txt must NOT appear in the
    captured diff; the implementer's edit to B.txt must."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    # Pre-existing dirt (another session), present BEFORE the baseline snapshot.
    (repo / "A.txt").write_text("A CONCURRENT-SESSION EDIT\n")

    # Run start: snapshot the tree.
    ex._capture_pre_impl_baseline(str(run_dir))
    assert (run_dir / "pre-implementer-ref").is_file()

    # Implementer edits B in place.
    (repo / "B.txt").write_text("B IMPLEMENTER EDIT\n")

    # Reviewer input assembly generates review-diff.patch.
    ex._assemble_reviewer_inputs(str(run_dir))
    diff = (run_dir / "review-diff.patch").read_text()

    assert "B IMPLEMENTER EDIT" in diff, "implementer's own change must be captured"
    assert "CONCURRENT-SESSION EDIT" not in diff, "pre-existing dirt must be excluded"
    assert "A.txt" not in diff, "the pre-existing dirty file must not appear at all"


def test_capture_on_clean_tree_still_captures_implementer(tmp_path, monkeypatch):
    """No regression on the happy path: with a clean tree at baseline (== HEAD),
    the implementer's change is still captured."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    ex._capture_pre_impl_baseline(str(run_dir))
    (repo / "B.txt").write_text("B IMPLEMENTER EDIT\n")
    ex._assemble_reviewer_inputs(str(run_dir))
    diff = (run_dir / "review-diff.patch").read_text()

    assert "B IMPLEMENTER EDIT" in diff


def test_baseline_is_idempotent(tmp_path, monkeypatch):
    """The baseline is captured once (before the first implementer iteration) and
    not overwritten by later calls in a revision loop."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_baseline(str(run_dir))
    first = (run_dir / "pre-implementer-ref").read_text()
    # A later edit + second call must not move the baseline.
    (repo / "A.txt").write_text("later dirt\n")
    ex._capture_pre_impl_baseline(str(run_dir))
    assert (run_dir / "pre-implementer-ref").read_text() == first
