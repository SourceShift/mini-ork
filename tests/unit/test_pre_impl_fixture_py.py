"""Regression: the pre-implementer baseline must survive gc and vendor a
minimal, git-sourced fixture directory.

``_capture_pre_impl_baseline`` snapshots the tree before the implementer runs
via a non-destructive ``git stash create``. That commit is a dangling object —
``git gc`` reaps it, so 22 of 30 recorded refs were already unresolvable. The
fix pins it to ``refs/mo/pre-impl/<run_id>`` and snapshots the changed files'
pre-edit content into ``<run_dir>/pre-impl-fixture/``. These tests drive the
real functions against a real throwaway repo — no mocks.
"""
from __future__ import annotations

import json
import os
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
    (repo / "a.py").write_text("V = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")


def _run_id(run_dir: Path) -> str:
    return os.path.basename(str(run_dir).rstrip(os.sep))


def test_ref_survives_gc(tmp_path, monkeypatch):
    """The pinned ref survives ``git gc --prune=now`` — the load-bearing fix.

    A dirty tree at capture makes ``git stash create`` return a real dangling
    commit rather than HEAD, so without the pin gc would reap it.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("V = 0\n")  # pre-existing dirt → non-empty stash
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_baseline(str(run_dir))
    _git(repo, "gc", "--prune=now")
    ref = f"refs/mo/pre-impl/{_run_id(run_dir)}"
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-t", ref],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"{ref} did not survive gc: {proc.stderr}"
    assert proc.stdout.strip() == "commit"


def test_manifest_and_pre_edit_body(tmp_path, monkeypatch):
    """The manifest lists changed files and the pre-edit (committed) body."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_baseline(str(run_dir))  # clean tree → baseline = HEAD

    (repo / "a.py").write_text("V = 2\n")  # implementer edit
    (repo / "b.py").write_text("NEW\n")    # implementer-created file

    ex._capture_pre_impl_fixture(str(run_dir), str(repo))

    manifest_path = run_dir / "pre-impl-fixture" / "MANIFEST.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())

    baseline = (run_dir / "pre-implementer-ref").read_text().strip()
    assert manifest["baseline_ref"] == baseline
    assert manifest["changed_files"] == ["a.py", "b.py"]
    assert manifest["created_by_run"] == ["b.py"]

    # The modified file's pre-edit content is the COMMITTED version, not the
    # working-tree version.
    body = (run_dir / "pre-impl-fixture" / "files" / "a.py").read_bytes()
    assert body == b"V = 1\n"
    # A newly created file has no fixture entry (and no zero-byte file).
    assert not (run_dir / "pre-impl-fixture" / "files" / "b.py").exists()


def test_fixture_is_idempotent(tmp_path, monkeypatch):
    """A second call is a no-op: the first capture wins."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_baseline(str(run_dir))
    (repo / "a.py").write_text("V = 2\n")
    ex._capture_pre_impl_fixture(str(run_dir), str(repo))

    manifest_path = run_dir / "pre-impl-fixture" / "MANIFEST.json"
    first = manifest_path.read_bytes()
    first_mtime = manifest_path.stat().st_mtime_ns

    (repo / "a.py").write_text("V = 3\n")
    ex._capture_pre_impl_fixture(str(run_dir), str(repo))

    assert manifest_path.read_bytes() == first
    assert manifest_path.stat().st_mtime_ns == first_mtime


def test_no_changed_files_still_valid_ref(tmp_path, monkeypatch):
    """A clean tree yields an empty changed_files manifest and a valid ref."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_baseline(str(run_dir))
    ex._capture_pre_impl_fixture(str(run_dir), str(repo))

    manifest_path = run_dir / "pre-impl-fixture" / "MANIFEST.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["changed_files"] == []
    assert manifest["created_by_run"] == []

    ref = f"refs/mo/pre-impl/{_run_id(run_dir)}"
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-t", ref],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0


def test_missing_ref_writes_nothing(tmp_path, monkeypatch):
    """No pre-implementer-ref → the fixture writes nothing."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))

    ex._capture_pre_impl_fixture(str(run_dir), str(repo))
    assert not (run_dir / "pre-impl-fixture").exists()
