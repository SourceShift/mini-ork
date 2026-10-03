"""Hermetic tests for ``mini_ork.acp.diffs`` (Z4).

No external state. Each test builds a temp git repo + a temp run dir with
the exact artifact layout the recipe writes (``implementer-summary.json``,
``pre-implementer-ref``, ``pre-impl-fixture/files/<rel>``) and asserts the
``run_diffs`` / ``cached_or_computed`` contract end-to-end.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp.diffs import (  # noqa: E402
    CACHE_NAME,
    PLACEHOLDER,
    cached_or_computed,
    run_diffs,
)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A temp git repo with one tracked file and a known initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = subprocess.run
    run(["git", "-C", str(repo), "init", "-q"], check=True, env={
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
    })
    (repo / "tracked.txt").write_text("hello\n", encoding="utf-8")
    run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True, env={
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
    })
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (tmp_path / "baseline.txt").write_text(head, encoding="utf-8")
    return repo


def _write_summary(run_dir: Path, worktree: Path, files: list[str], status: str = "implemented") -> None:
    payload = {
        "status": status,
        "worktree_path": str(worktree),
        "files_changed": files,
        "implementation_log": "",
    }
    (run_dir / "implementer-summary.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_baseline(run_dir: Path, baseline: str) -> None:
    (run_dir / "pre-implementer-ref").write_text(baseline + "\n", encoding="utf-8")


# ── modified file ────────────────────────────────────────────────────────────


def test_modified_file_returns_old_and_new_texts(git_repo: Path, tmp_path: Path):
    # Tracked file: change it on disk AFTER the baseline commit.
    (git_repo / "tracked.txt").write_text("hello, world\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [str(git_repo / "tracked.txt")])
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    out = run_diffs(run_dir)
    assert len(out) == 1
    assert out[0]["path"] == str(git_repo / "tracked.txt")
    assert out[0]["old_text"] == "hello\n"
    assert out[0]["new_text"] == "hello, world\n"


# ── new file ─────────────────────────────────────────────────────────────────


def test_new_file_yields_old_text_none(git_repo: Path, tmp_path: Path):
    new = git_repo / "created.md"
    new.write_text("# fresh\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [str(new)])
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    out = run_diffs(run_dir)
    assert len(out) == 1
    assert out[0]["old_text"] is None
    assert out[0]["new_text"] == "# fresh\n"


# ── untracked file (fixture fallback) ────────────────────────────────────────


def test_untracked_file_with_fixture_uses_fixture_copy(git_repo: Path, tmp_path: Path):
    # File is untracked in the repo (never added to git) — the implementer
    # creates it; only the pre-impl-fixture copy has the prior contents.
    new = git_repo / "scoped.txt"
    new.write_text("after\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    fixture_dir = run_dir / "pre-impl-fixture" / "files"
    # Mirror the writer's layout: <run_dir>/pre-impl-fixture/files/<rel path under worktree>.
    (fixture_dir / "scoped.txt").parent.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "scoped.txt").write_text("before\n", encoding="utf-8")
    _write_summary(run_dir, git_repo, [str(new)])
    # No baseline — git show would fail; fixture fallback must win.
    (run_dir / "pre-implementer-ref").write_text("\n", encoding="utf-8")

    out = run_diffs(run_dir)
    assert len(out) == 1
    assert out[0]["old_text"] == "before\n"
    assert out[0]["new_text"] == "after\n"


# ── deleted file ─────────────────────────────────────────────────────────────


def test_deleted_file_yields_empty_new_text(git_repo: Path, tmp_path: Path):
    tracked = git_repo / "tracked.txt"
    tracked.unlink()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [str(tracked)])
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    out = run_diffs(run_dir)
    assert len(out) == 1
    assert out[0]["new_text"] == ""
    assert out[0]["old_text"] == "hello\n"


# ── outside-worktree path is skipped ────────────────────────────────────────


def test_outside_worktree_path_is_skipped(git_repo: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [str(outside)])
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    assert run_diffs(run_dir) == []


# ── large / binary files ─────────────────────────────────────────────────────


def test_large_file_yields_placeholder(git_repo: Path, tmp_path: Path):
    huge = git_repo / "big.txt"
    huge.write_text("a" * 300_000, encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [str(huge)])
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    out = run_diffs(run_dir)
    assert len(out) == 1
    # Never the placeholder on both sides: identical texts read as "no change".
    assert out[0]["old_text"] is None
    assert out[0]["new_text"] == PLACEHOLDER


def test_binary_file_yields_placeholder(git_repo: Path, tmp_path: Path):
    binfile = git_repo / "blob.bin"
    binfile.write_bytes(b"\x00\x01\x02\x03\xff\xfe")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [str(binfile)])
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    out = run_diffs(run_dir)
    assert len(out) == 1
    # Never the placeholder on both sides: identical texts read as "no change".
    assert out[0]["old_text"] is None
    assert out[0]["new_text"] == PLACEHOLDER


# ── max_files cap ────────────────────────────────────────────────────────────


def test_max_files_caps_the_output(git_repo: Path, tmp_path: Path):
    files = []
    for i in range(25):
        p = git_repo / f"f{i}.txt"
        p.write_text(f"{i}\n", encoding="utf-8")
        files.append(str(p))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, files)
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    out = run_diffs(run_dir, max_files=20)
    assert len(out) == 20


# ── no_changes / missing summary ────────────────────────────────────────────


def test_no_changes_status_returns_empty(git_repo: Path, tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [], status="no_changes")

    assert run_diffs(run_dir) == []


def test_missing_summary_returns_empty(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert run_diffs(run_dir) == []


# ── git failure never raises ────────────────────────────────────────────────


def test_git_failure_does_not_raise(tmp_path: Path):
    # Point worktree at a non-existent path so ``git -C`` fails outright.
    bogus = tmp_path / "missing"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, bogus, [str(bogus / "x.txt")])
    _write_baseline(run_dir, "deadbeef" * 5)

    assert run_diffs(run_dir) == []


# ── cache write + replay ────────────────────────────────────────────────────


def test_cache_is_written_when_diffs_non_empty(git_repo: Path, tmp_path: Path):
    (git_repo / "tracked.txt").write_text("hi\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [str(git_repo / "tracked.txt")])
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    out1 = run_diffs(run_dir)
    assert out1 and (run_dir / CACHE_NAME).is_file()

    out2, from_cache = cached_or_computed(run_dir)
    assert from_cache is True
    assert out2 == out1


def test_cached_or_computed_falls_through_when_cache_missing(git_repo: Path, tmp_path: Path):
    (git_repo / "tracked.txt").write_text("hi\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_summary(run_dir, git_repo, [str(git_repo / "tracked.txt")])
    _write_baseline(run_dir, (tmp_path / "baseline.txt").read_text().strip())

    out, from_cache = cached_or_computed(run_dir)
    assert from_cache is False
    assert out and out[0]["new_text"] == "hi\n"
    # Computed from today's files, so never cached: the cache holds only what
    # the run itself produced (written by run_diffs at the implementer's end).
    assert not (run_dir / CACHE_NAME).exists()


def test_cached_or_computed_ignores_unparseable_cache(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / CACHE_NAME).write_text("not json", encoding="utf-8")
    # No summary → still [], but the function must not raise.
    out, from_cache = cached_or_computed(run_dir)
    assert out == []
    assert from_cache is False