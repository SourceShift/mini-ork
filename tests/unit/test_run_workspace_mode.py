"""Pure-helper tests for ``mini-ork run``'s workspace-mode decision and
worktree isolation (Zed S4 IPC, kickoff ``hr-cli-worktree``).

Tests hit the two pure helpers in ``mini_ork.cli.main`` — ``_resolve_workspace_mode``
and ``_isolate_run`` — directly, so the lifecycle's pre-parse loop, the
``--set`` publish, the budget preflight, the classifier child, ``gen_profile``
and the rest never run. No LLM calls; no subprocess; no real ``mini-ork``
engine root. The git worktree seam (``workspaces.create``) is real
subprocess, mirroring ``test_workspaces.py`` so ff-vs-no-ff and
``git worktree add`` failures surface as failures rather than mock-vs-real
divergences.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mini_ork import workspaces as ws_mod
from mini_ork.cli import main as cli_main
from mini_ork.gates.review_policy import ReviewPolicy


# ── git + home helpers ─────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(tmp_path: Path, *, name: str = "proj") -> Path:
    """Fresh temp git repo with one commit, returned by path.

    Mirrors ``tests/unit/test_workspaces.py::_init_repo`` so the worktree
    IPC is exercised against the same fixture shape the workspaces suite
    uses.
    """
    project = tmp_path / name
    project.mkdir()
    assert _git(["init", "-b", "main"], project).returncode == 0
    _git(["config", "user.name", "Test"], project)
    _git(["config", "user.email", "test@example.com"], project)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    assert _git(["add", "README.md"], project).returncode == 0
    assert _git(["commit", "-m", "init"], project).returncode == 0
    return project


def _init_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _patch_worktree_dir(monkeypatch, tmp_path: Path) -> Path:
    """Pin ``MO_WORKTREE_DIR`` to a tmp dir so worktrees land under pytest.

    Without this, ``workspaces.worktree_dir`` reads ``ZED_SETTINGS`` or the
    developer's ``~/.config/zed/settings.json`` and could write worktrees
    into the developer's real home directory.
    """
    wt = tmp_path / "wts"
    wt.mkdir()
    monkeypatch.setenv("MO_WORKTREE_DIR", str(wt))
    return wt


@pytest.fixture(autouse=True)
def _scrub_workspace_env(monkeypatch, tmp_path):
    """Scrub every workspace / target / home env so the test sees a clean slate.

    ``MO_WORKSPACE_MODE``, ``MO_REVIEW_BEFORE_MERGE``, ``MO_TARGET_CWD``,
    ``MINI_ORK_HOME``, ``ZED_SETTINGS`` — and pin ``MO_WORKTREE_DIR`` to a
    tmp dir so workspaces.create doesn't try to write into a developer's
    real Zed settings path. The autouse scope keeps each test hermetic; the
    resolution tests already pass an explicit ``env`` mapping, but the
    ``_isolate_run`` tests want ``workspaces.worktree_dir`` to be tame.
    """
    monkeypatch.delenv("MO_WORKSPACE_MODE", raising=False)
    monkeypatch.delenv("MO_REVIEW_BEFORE_MERGE", raising=False)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("ZED_SETTINGS", raising=False)
    _patch_worktree_dir(monkeypatch, tmp_path)
    yield


# ── _resolve_workspace_mode ────────────────────────────────────────────────


def test_rwm_mode_default_is_in_place():
    """No flag, no env, policy ``off`` → ``in-place`` from source ``default``."""
    mode, source, rest = cli_main._resolve_workspace_mode([], {}, ReviewPolicy(before_merge="off"))
    assert mode == "in-place"
    assert source == "default"
    assert rest == []


def test_rwm_mode_policy_required_yields_worktree():
    """No flag, no env, policy ``required`` → ``worktree`` from source ``policy``."""
    mode, source, rest = cli_main._resolve_workspace_mode(
        [], {}, ReviewPolicy(before_merge="required"))
    assert mode == "worktree"
    assert source == "policy"
    assert rest == []


def test_rwm_mode_env_in_place_beats_policy_required():
    """``MO_WORKSPACE_MODE=in-place`` overrides a ``required`` policy."""
    mode, source, rest = cli_main._resolve_workspace_mode(
        [], {"MO_WORKSPACE_MODE": "in-place"}, ReviewPolicy(before_merge="required"))
    assert mode == "in-place"
    assert source == "env"
    assert rest == []


def test_rwm_mode_flag_worktree_beats_env_and_is_stripped():
    """``--workspace worktree`` overrides env; the flag is gone from argv_rest."""
    mode, source, rest = cli_main._resolve_workspace_mode(
        ["--workspace", "worktree"],
        {"MO_WORKSPACE_MODE": "in-place"},
        None)
    assert mode == "worktree"
    assert source == "flag"
    assert rest == []


def test_rwm_mode_flag_equals_form_also_strips():
    """``--workspace=worktree`` parses the same and strips the same."""
    mode, source, rest = cli_main._resolve_workspace_mode(
        ["--workspace=worktree"], {}, ReviewPolicy(before_merge="off"))
    assert mode == "worktree"
    assert source == "flag"
    assert rest == []


def test_rwm_mode_flag_keeps_following_positional():
    """After stripping, the kickoff / recipe positional stays in argv_rest."""
    mode, source, rest = cli_main._resolve_workspace_mode(
        ["--workspace", "worktree", "kickoff.md"], {}, None)
    assert mode == "worktree"
    assert source == "flag"
    assert rest == ["kickoff.md"]


def test_rwm_mode_illegal_flag_raises_valueerror():
    """An unknown ``--workspace`` value raises with a clear message."""
    with pytest.raises(ValueError, match=r"--workspace: expected worktree\|in-place \(got 'sideways'\)"):
        cli_main._resolve_workspace_mode(["--workspace", "sideways"], {}, None)


def test_rwm_mode_missing_flag_value_raises_valueerror():
    """``--workspace`` with no following argument raises (mirror of ``--placement``)."""
    with pytest.raises(ValueError, match=r"--workspace requires <mode>"):
        cli_main._resolve_workspace_mode(["--workspace"], {}, None)


def test_rwm_mode_illegal_env_value_raises_valueerror():
    """An unknown ``MO_WORKSPACE_MODE`` value raises with a clear message."""
    with pytest.raises(
        ValueError, match=r"MO_WORKSPACE_MODE: expected worktree\|in-place \(got 'sideways'\)"
    ):
        cli_main._resolve_workspace_mode([], {"MO_WORKSPACE_MODE": "sideways"}, None)


def test_rwm_mode_empty_env_value_is_treated_as_unset():
    """Whitespace / empty ``MO_WORKSPACE_MODE`` is treated as unset; policy decides."""
    mode, source, _ = cli_main._resolve_workspace_mode(
        [], {"MO_WORKSPACE_MODE": "   "}, ReviewPolicy(before_merge="required"))
    assert mode == "worktree"
    assert source == "policy"


def test_rwm_mode_none_policy_means_default():
    """``policy=None`` falls through to the default; the helper is permissive."""
    mode, source, _ = cli_main._resolve_workspace_mode([], {}, None)
    assert mode == "in-place"
    assert source == "default"


def test_rwm_mode_duck_typed_policy_is_accepted():
    """A duck-typed policy with ``before_merge`` works the same as a real one."""
    class _Fake:
        before_merge = "required"

    mode, source, _ = cli_main._resolve_workspace_mode([], {}, _Fake())
    assert mode == "worktree"
    assert source == "policy"


# ── _isolate_run ───────────────────────────────────────────────────────────


def test_rwm_isolate_mints_worktree_on_tmp_git_repo(tmp_path, capsys):
    """A fresh tmp repo + worktree-mode isolation mints a worktree.

    Asserts: the record exists, the new path is a worktree on
    ``mini-ork/<run_id>``, ``git worktree list`` shows it, the original
    checkout's ``git status --porcelain`` is unchanged, and the success
    line lands on stderr.
    """
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("hello\n", encoding="utf-8")
    pre_status = _git(["status", "--porcelain"], project).stdout

    ok, ws = cli_main._isolate_run(
        project, home, "run-1700-isolate", str(kickoff), "flag")

    assert ok is True
    assert ws is not None
    # Record exists via workspaces.load.
    assert ws_mod.load(home, "run-1700-isolate") is not None
    # New path is a real git worktree on the expected branch.
    assert ws.branch == "mini-ork/run-1700-isolate"
    listed = _git(["worktree", "list"], project).stdout
    assert "run-1700-isolate" in listed
    # Original checkout is untouched.
    assert _git(["status", "--porcelain"], project).stdout == pre_status
    # Success line landed on stderr.
    err = capsys.readouterr().err
    assert "isolated in" in err
    assert "run-1700-isolate" in err
    assert "mini-ork/run-1700-isolate" in err
    assert "merge needs review" in err


def test_rwm_isolate_skips_when_record_already_exists(tmp_path, capsys):
    """Pre-created record → no second worktree; ``(True, None)`` returned.

    Verifies the skip-silently rule: Zed thread or automation already owns
    the run, so the new lifecycle must not mint a second worktree.
    """
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("hello\n", encoding="utf-8")

    existing = ws_mod.create(project, home, "run-1700-skip")
    listed_before = _git(["worktree", "list"], project).stdout

    ok, ws = cli_main._isolate_run(
        project, home, "run-1700-skip", str(kickoff), "policy")

    assert ok is True
    assert ws is None
    # No second worktree.
    assert _git(["worktree", "list"], project).stdout == listed_before
    # Existing record still present.
    assert ws_mod.load(home, "run-1700-skip") is not None
    # The pre-existing workspace's path is the recorded one.
    record = json.loads((home / "worktrees" / "run-1700-skip.json").read_text())
    assert Path(record["path"]) == existing.path
    # No success line was emitted (skip-silently).
    assert capsys.readouterr().err == ""


def test_rwm_isolate_non_git_target_returns_failure_message(tmp_path):
    """A non-git target with source ``policy`` → ``(False, msg)`` mentioning review."""
    target = tmp_path / "plain"
    target.mkdir()
    home = _init_home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("hello\n", encoding="utf-8")

    ok, msg = cli_main._isolate_run(
        target, home, "run-1700-fail", str(kickoff), "policy")

    assert ok is False
    assert "review required" in msg
    assert str(target) in msg


def test_rwm_isolate_non_git_target_under_flag_also_fails(tmp_path):
    """A non-git target with source ``flag`` is also fail-closed (no silent fallback)."""
    target = tmp_path / "plain"
    target.mkdir()
    home = _init_home(tmp_path)
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("hello\n", encoding="utf-8")

    ok, msg = cli_main._isolate_run(
        target, home, "run-1700-flagfail", str(kickoff), "flag")

    assert ok is False
    assert "review required" in msg


# ── constants ──────────────────────────────────────────────────────────────


def test_rwm_workspace_values_match_documented_enum():
    """The local ``_WORKSPACE_VALUES`` matches the engine setting token set."""
    assert cli_main._WORKSPACE_WORKTREE == "worktree"
    assert cli_main._WORKSPACE_IN_PLACE == "in-place"
    assert cli_main._WORKSPACE_VALUES == ("worktree", "in-place")
