"""Pinned run roots: resolve once, persist, replay (``mini_ork.runtime.run_roots``).

Remote-nodes-01 epic: the four consumers (baseline, implementer, verifier cwd,
publisher target) all read ``MO_TARGET_CWD`` lazily today. The pin closes the
baseline-vs-implementer tree-drift bug by resolving the run's four roots once,
writing them to ``run_profile.json["roots"]``, and replaying them everywhere.

These tests drive the real helpers against real throwaway git repos — no
mocks. Three groups:
  * Parity — ``resolve_run_roots(...)`` is byte-identical to the legacy
    ``_resolve_target_cwd`` across all four precedence branches.
  * Persistence / idempotence — writing twice is a no-op; existing record
    wins over live ``MO_TARGET_CWD`` (resume-keeps-original-roots).
  * Divergence — cwd set to an unrelated repo while the kickoff sits inside
    the target repo: the pinned record forces both the baseline and the
    implementer to read the target, NOT cwd (kickoff acceptance #1).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.runtime import run_roots  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")


def _write_profile(run_dir: Path, *, kickoff_path: str = "", roots: dict | None = None) -> None:
    """Write a minimal run_profile.json with the given kickoff_path (and optional roots)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    prof: dict[str, object] = {"kickoff_path": kickoff_path}
    if roots is not None:
        prof["roots"] = roots
    (run_dir / "run_profile.json").write_text(json.dumps(prof, indent=2))


# ─── Parity: four precedence branches must agree with the legacy helper ─────


def test_parity_explicit_mo_target_cwd_inside_a_repo(tmp_path, monkeypatch):
    """Branch 1: explicit MO_TARGET_CWD inside a repo → git toplevel."""
    repo = tmp_path / "explicit"
    _init_repo(repo)
    subdir = repo / "nested"
    subdir.mkdir()
    kickoff = subdir / "kickoff.md"
    kickoff.write_text("k")
    run_dir = tmp_path / "run"
    _write_profile(run_dir, kickoff_path=str(kickoff))
    monkeypatch.setenv("MO_TARGET_CWD", str(subdir))
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    legacy = ex._resolve_target_cwd(str(run_dir))
    pinned = run_roots.resolve_run_roots(str(run_dir)).target
    assert legacy == pinned == str(repo)


def test_parity_kickoff_in_a_repo_with_no_mo_target_cwd(tmp_path, monkeypatch):
    """Branch 2: no MO_TARGET_CWD, kickoff inside a repo → kickoff's git toplevel."""
    repo = tmp_path / "kickoff-repo"
    _init_repo(repo)
    kickoff = repo / "K.md"
    kickoff.write_text("k")
    run_dir = tmp_path / "run"
    _write_profile(run_dir, kickoff_path=str(kickoff))
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    legacy = ex._resolve_target_cwd(str(run_dir))
    pinned = run_roots.resolve_run_roots(str(run_dir)).target
    assert legacy == pinned == str(repo)


def test_parity_kickoff_not_in_a_repo_falls_back_to_kickoff_dir(tmp_path, monkeypatch):
    """Branch 3: kickoff path exists but its dir is NOT a repo → kickoff dir."""
    bare = tmp_path / "norepo"
    bare.mkdir()
    kickoff = bare / "K.md"
    kickoff.write_text("k")
    run_dir = tmp_path / "run"
    _write_profile(run_dir, kickoff_path=str(kickoff))
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    legacy = ex._resolve_target_cwd(str(run_dir))
    pinned = run_roots.resolve_run_roots(str(run_dir)).target
    assert legacy == pinned == str(bare)


def test_parity_no_kickoff_no_target_returns_explicit_or_cwd(tmp_path, monkeypatch):
    """Branch 4: no kickoff, no MO_TARGET_CWD → cwd (last fallback)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # No run_profile.json → no kickoff_path.
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    # Run inside an arbitrary non-git path.
    cwd = tmp_path / "somewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    legacy = ex._resolve_target_cwd(str(run_dir))
    pinned = run_roots.resolve_run_roots(str(run_dir)).target
    assert legacy == pinned == str(cwd)


# ─── Persistence / idempotence ───────────────────────────────────────────────


def test_persist_writes_roots_only_once(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    kickoff = repo / "K.md"
    kickoff.write_text("k")
    run_dir = tmp_path / "run"
    _write_profile(run_dir, kickoff_path=str(kickoff))
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    first = run_roots.persist_run_roots(str(run_dir))
    assert first is not None
    assert first.target == str(repo)

    second = run_roots.persist_run_roots(str(run_dir))
    assert second is not None
    assert second.target == str(repo)


def test_resume_keeps_original_roots_when_mo_target_cwd_changes(tmp_path, monkeypatch):
    """Acceptance #2: a resumed run reuses the persisted roots even if MO_TARGET_CWD
    changed between cycles."""
    original = tmp_path / "original"
    _init_repo(original)
    other = tmp_path / "other"
    _init_repo(other)
    kickoff = original / "K.md"
    kickoff.write_text("k")
    run_dir = tmp_path / "run"
    _write_profile(run_dir, kickoff_path=str(kickoff))
    monkeypatch.setenv("MO_TARGET_CWD", str(original))
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    # First cycle: pin to the original repo.
    run_roots.persist_run_roots(str(run_dir))
    first = run_roots.load_run_roots(str(run_dir))
    assert first is not None
    assert first.target == str(original)

    # Resumed cycle: env now points at a different repo, but the persisted
    # record wins.
    monkeypatch.setenv("MO_TARGET_CWD", str(other))
    again = run_roots.persist_run_roots(str(run_dir))
    assert again is not None
    assert again.target == str(original)


def test_load_run_roots_returns_none_for_legacy_run_dir(tmp_path):
    """No ``roots`` key in run_profile.json → None (legacy fallback)."""
    run_dir = tmp_path / "legacy"
    run_dir.mkdir()
    _write_profile(run_dir, kickoff_path="")  # no roots key
    assert run_roots.load_run_roots(str(run_dir)) is None


def test_load_run_roots_returns_none_for_missing_run_dir(tmp_path):
    assert run_roots.load_run_roots("") is None
    assert run_roots.load_run_roots(str(tmp_path / "absent")) is None


# ─── Divergence: pinned record rescues baseline + implementer from cwd drift


def test_pinned_record_divorces_baseline_from_executor_cwd(tmp_path, monkeypatch):
    """Acceptance #1: executor cwd is an unrelated repo, the kickoff sits inside
    the target repo — ``_capture_pre_impl_baseline`` must snapshot the TARGET,
    NOT cwd."""
    target = tmp_path / "target"
    _init_repo(target)
    other = tmp_path / "other"
    _init_repo(other)
    kickoff = target / "K.md"
    kickoff.write_text("k")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_profile(run_dir, kickoff_path=str(kickoff))
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    # Pin once (the implementer would normally do this before it edits).
    run_roots.persist_run_roots(str(run_dir))

    # Make the executor's cwd the unrelated repo.
    monkeypatch.chdir(other)

    ex._capture_pre_impl_baseline(str(run_dir))
    assert (run_dir / "pre-implementer-ref").is_file()
    # The persisted target wins over the executor's cwd.
    roots = run_roots.load_run_roots(str(run_dir))
    assert roots is not None
    assert roots.target == str(target)


# ─── exec_cwd: only recorded when the shared-drive backend is opted in ──────


def test_exec_cwd_absent_by_default(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    kickoff = repo / "K.md"
    kickoff.write_text("k")
    run_dir = tmp_path / "run"
    _write_profile(run_dir, kickoff_path=str(kickoff))
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    roots = run_roots.resolve_run_roots(str(run_dir))
    assert roots.exec_cwd is None


def test_exec_cwd_recorded_when_shared_drive_opted_in(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    drive_root = tmp_path / "drive"
    kickoff = repo / "K.md"
    kickoff.write_text("k")
    run_dir = tmp_path / "run"
    _write_profile(run_dir, kickoff_path=str(kickoff))
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.setenv("MO_SHARED_DRIVE_BACKEND", "local-bind")
    monkeypatch.setenv("MO_SHARED_DRIVE_ROOT", str(drive_root))
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    roots = run_roots.resolve_run_roots(str(run_dir))
    assert roots.exec_cwd == os.path.abspath(str(drive_root))
    # The target stays the resolved git toplevel — not the drive redirect.
    assert roots.target == str(repo)