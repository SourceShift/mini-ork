"""Fail-closed run-root resolution (RBF-01).

Run-1791930074-82118's profile named a target worktree that did not exist.
``resolve_run_roots`` dropped the dead path silently and took the kickoff's
toplevel — the engine's primary checkout — so the implementer edited the
engine's frozen fixture and the revived diff swept in engine files. These
tests pin the two refusals that close that hazard:

  * a named target (``MO_TARGET_CWD``, ``MINI_ORK_TARGET_REPO`` or the
    profile's ``target_repo``) that is not an existing directory raises
    ``RunRootsError`` instead of falling back to the kickoff's toplevel;
  * a target whose git toplevel is the engine's raises unless
    ``MO_ALLOW_FRAMEWORK_CWD=1`` acknowledges a self-edit.

Hermetic: throwaway git repos, explicit ``env`` mappings, no network, no live
run state. ``persist_run_roots`` is exercised against a throwaway sqlite DB so
the "message in the run's notes" duty is proven without touching the
operator's state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess

import pytest

from mini_ork.runtime import run_roots


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _init_repo(path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", str(path)],
                   check=True, capture_output=True, text=True, env=env)
    return str(path)


def _write_profile(run_dir, **fields) -> str:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_profile.json").write_text(json.dumps(fields), encoding="utf-8")
    return str(run_dir)


def _kickoff_in(repo: str) -> str:
    kickoff = os.path.join(repo, "kickoff.md")
    with open(kickoff, "w", encoding="utf-8") as fh:
        fh.write("# kickoff\n")
    return kickoff


def _assert_names_missing(msg: str, missing: str) -> None:
    assert "does not exist" in msg, msg
    assert missing in msg or os.path.realpath(missing) in msg, msg


def _home_staged_run(tmp_path, *, target_repo: str = "") -> tuple[str, str, str]:
    """A run whose kickoff is staged under ``<engine>/.mini-ork`` (declares no
    target), plus an unrelated existing repo to try to smuggle in as a target."""
    engine = _init_repo(tmp_path / "engine")
    home = os.path.join(engine, ".mini-ork")
    child = os.path.join(home, "runs", "child")
    os.makedirs(child)
    kickoff = os.path.join(child, "kickoff.md")
    with open(kickoff, "w", encoding="utf-8") as fh:
        fh.write("# child\n")
    fields = {"kickoff_path": kickoff}
    if target_repo:
        fields["target_repo"] = target_repo
    run_dir = _write_profile(tmp_path / "child-run", **fields)
    return run_dir, engine, home


# ---------------------------------------------------------------------------
# The exception
# ---------------------------------------------------------------------------


def test_run_roots_error_is_a_value_error():
    assert issubclass(run_roots.RunRootsError, ValueError)


# ---------------------------------------------------------------------------
# A named target that does not exist fails closed
# ---------------------------------------------------------------------------


def test_missing_mo_target_cwd_raises_instead_of_kickoff_toplevel(tmp_path):
    """The live RB-01 shape: the profile's target is gone, the kickoff's repo
    must NOT become the edit surface."""
    kick_repo = _init_repo(tmp_path / "kickrepo")
    kickoff = _kickoff_in(kick_repo)
    run_dir = _write_profile(tmp_path / "run", kickoff_path=kickoff)
    missing = str(tmp_path / "worktrees" / "e1-scratch")

    with pytest.raises(run_roots.RunRootsError) as excinfo:
        run_roots.resolve_run_roots(run_dir, env={"MO_TARGET_CWD": missing})
    _assert_names_missing(str(excinfo.value), missing)


def test_missing_profile_target_repo_raises(tmp_path):
    kick_repo = _init_repo(tmp_path / "kickrepo")
    kickoff = _kickoff_in(kick_repo)
    missing = str(tmp_path / "gone" / "e1-scratch")
    run_dir = _write_profile(tmp_path / "run", kickoff_path=kickoff,
                             target_repo=missing)

    with pytest.raises(run_roots.RunRootsError) as excinfo:
        run_roots.resolve_run_roots(run_dir, env={})
    _assert_names_missing(str(excinfo.value), missing)


def test_missing_mini_ork_target_repo_raises(tmp_path):
    missing = str(tmp_path / "vanished-repo")

    with pytest.raises(run_roots.RunRootsError) as excinfo:
        run_roots.resolve_run_roots(str(tmp_path / "run"),
                                    env={"MINI_ORK_TARGET_REPO": missing})
    _assert_names_missing(str(excinfo.value), missing)


# ---------------------------------------------------------------------------
# The engine-coincidence guard and its acknowledgement flag
# ---------------------------------------------------------------------------


def _engine_with_subdir(tmp_path):
    engine = _init_repo(tmp_path / "engine")
    sub = tmp_path / "engine" / "sub"
    sub.mkdir()
    return engine, sub


def test_engine_coincident_target_without_flag_raises(tmp_path):
    engine, sub = _engine_with_subdir(tmp_path)

    with pytest.raises(run_roots.RunRootsError) as excinfo:
        run_roots.resolve_run_roots(str(tmp_path / "run"), env={
            "MO_TARGET_CWD": str(sub), "MINI_ORK_ROOT": engine,
        })
    message = str(excinfo.value)
    assert "engine checkout" in message, message
    assert "MO_ALLOW_FRAMEWORK_CWD=1" in message, message


def test_engine_coincident_target_with_flag_resolves(tmp_path):
    engine, sub = _engine_with_subdir(tmp_path)

    roots = run_roots.resolve_run_roots(str(tmp_path / "run"), env={
        "MO_TARGET_CWD": str(sub), "MINI_ORK_ROOT": engine,
        "MO_ALLOW_FRAMEWORK_CWD": "1",
    })
    assert os.path.realpath(roots.target) == os.path.realpath(engine)


@pytest.mark.parametrize("flag", ["0", "true", "yes", ""])
def test_engine_guard_requires_exactly_flag_one(tmp_path, flag):
    engine, sub = _engine_with_subdir(tmp_path)

    with pytest.raises(run_roots.RunRootsError):
        run_roots.resolve_run_roots(str(tmp_path / "run"), env={
            "MO_TARGET_CWD": str(sub), "MINI_ORK_ROOT": engine,
            "MO_ALLOW_FRAMEWORK_CWD": flag,
        })


# ---------------------------------------------------------------------------
# Regressions: everything that resolved before still resolves
# ---------------------------------------------------------------------------


def test_existing_explicit_target_outside_the_engine_is_unchanged(tmp_path):
    engine = _init_repo(tmp_path / "engine")
    operand = _init_repo(tmp_path / "operand")

    roots = run_roots.resolve_run_roots(str(tmp_path / "run"), env={
        "MO_TARGET_CWD": operand, "MINI_ORK_ROOT": engine,
    })
    assert isinstance(roots, run_roots.RunRoots)
    assert os.path.realpath(roots.target) == os.path.realpath(operand)


def test_kickoff_inferred_target_still_resolves(tmp_path):
    """No target is named at all: the kickoff's own toplevel is (unchanged)."""
    engine = _init_repo(tmp_path / "engine")
    kick_repo = _init_repo(tmp_path / "kickrepo")
    kickoff = _kickoff_in(kick_repo)
    run_dir = _write_profile(tmp_path / "run", kickoff_path=kickoff)

    roots = run_roots.resolve_run_roots(run_dir, env={"MINI_ORK_ROOT": engine})
    assert os.path.realpath(roots.target) == os.path.realpath(kick_repo)


def test_home_staged_kickoff_with_nothing_named_keeps_the_launch_tree(tmp_path):
    """A home-staged kickoff declares no target (module doctrine): with no
    named target the launch tree stands, even when it is the engine. The
    epic-runner's unpinned child resolves exactly this way."""
    engine = _init_repo(tmp_path / "engine")
    home = os.path.join(engine, ".mini-ork")
    os.makedirs(os.path.join(home, "runs", "child"))
    kickoff = os.path.join(home, "runs", "child", "kickoff.md")
    with open(kickoff, "w", encoding="utf-8") as fh:
        fh.write("# child\n")
    run_dir = _write_profile(tmp_path / "child-run", kickoff_path=kickoff)

    roots = run_roots.resolve_run_roots(run_dir, env={
        "MINI_ORK_HOME": home, "MINI_ORK_ROOT": engine,
    })
    assert os.path.realpath(roots.target) == os.path.realpath(engine)


# ---------------------------------------------------------------------------
# Spawned epic children: an inherited MINI_ORK_TARGET_REPO declares nothing
# ---------------------------------------------------------------------------


def test_spawned_child_ignores_an_inherited_target_repo(tmp_path):
    """`_child_env` copies the parent's MINI_ORK_TARGET_REPO but marks the child
    with MINI_ORK_PARENT_RUN_ID; the child edits the tree its own kickoff/home
    resolves, never the parent's edit surface (run-1791785967-43108)."""
    run_dir, engine, home = _home_staged_run(tmp_path)
    parent_surface = _init_repo(tmp_path / "parent-worktree")

    roots = run_roots.resolve_run_roots(run_dir, env={
        "MINI_ORK_HOME": home,
        "MINI_ORK_ROOT": engine,
        "MINI_ORK_TARGET_REPO": parent_surface,
        "MINI_ORK_PARENT_RUN_ID": "run-parent",
    })
    assert os.path.realpath(roots.target) == os.path.realpath(engine)


def test_spawned_child_does_not_fail_on_a_dead_inherited_target_repo(tmp_path):
    """The inherited value is not a declaration, so a parent worktree cleaned
    up after the parent started must not refuse the child."""
    run_dir, engine, home = _home_staged_run(tmp_path)

    roots = run_roots.resolve_run_roots(run_dir, env={
        "MINI_ORK_HOME": home,
        "MINI_ORK_ROOT": engine,
        "MINI_ORK_TARGET_REPO": str(tmp_path / "parent-worktree-cleaned"),
        "MINI_ORK_PARENT_RUN_ID": "run-parent",
    })
    assert os.path.realpath(roots.target) == os.path.realpath(engine)


def test_pinned_child_keeps_its_mo_target_cwd(tmp_path):
    """A child the dispatcher *pinned* (``_child_env(target=...)``) resolves to
    that pin even when an inherited MINI_ORK_TARGET_REPO is dead: only the
    winning declaration is checked (review finding 2)."""
    run_dir, engine, home = _home_staged_run(tmp_path)
    pinned = _init_repo(tmp_path / "child-worktree")

    roots = run_roots.resolve_run_roots(run_dir, env={
        "MINI_ORK_HOME": home,
        "MINI_ORK_ROOT": engine,
        "MO_TARGET_CWD": pinned,
        "MINI_ORK_TARGET_REPO": str(tmp_path / "parent-worktree-cleaned"),
        "MINI_ORK_PARENT_RUN_ID": "run-parent",
    })
    assert os.path.realpath(roots.target) == os.path.realpath(pinned)


def test_launcher_set_target_repo_still_declares_a_target(tmp_path):
    """Contrapositive of the child rule: without MINI_ORK_PARENT_RUN_ID the
    launcher's MINI_ORK_TARGET_REPO is a real declaration and moves the edit
    surface off a home-staged kickoff (tests/unit/test_run_roots.py:424)."""
    run_dir, engine, home = _home_staged_run(tmp_path)
    other = _init_repo(tmp_path / "other")

    roots = run_roots.resolve_run_roots(run_dir, env={
        "MINI_ORK_HOME": home,
        "MINI_ORK_ROOT": engine,
        "MINI_ORK_TARGET_REPO": other,
    })
    assert os.path.realpath(roots.target) == os.path.realpath(other)


def test_dead_lower_precedence_declaration_does_not_veto_the_winning_pin(tmp_path):
    """Only the highest-precedence declaration decides: a live MO_TARGET_CWD
    wins even when a lower-precedence profile target_repo is gone."""
    engine = _init_repo(tmp_path / "engine")
    operand = _init_repo(tmp_path / "operand")
    run_dir = _write_profile(
        tmp_path / "run", target_repo=str(tmp_path / "gone-target"))

    roots = run_roots.resolve_run_roots(run_dir, env={
        "MO_TARGET_CWD": operand, "MINI_ORK_ROOT": engine,
    })
    assert os.path.realpath(roots.target) == os.path.realpath(operand)


def test_missing_target_error_is_remediation_shaped(tmp_path):
    """Mid-run callers translate RunTargetMissing; a named target that is gone
    arrives as that type (still a RunRootsError) with how-to-fix text."""
    missing = str(tmp_path / "gone")

    with pytest.raises(run_roots.RunRootsError) as excinfo:
        run_roots.resolve_run_roots(str(tmp_path / "run"),
                                    env={"MO_TARGET_CWD": missing})
    assert isinstance(excinfo.value, run_roots.RunTargetMissing)
    message = str(excinfo.value)
    assert "does not exist" in message
    assert "MO_TARGET_CWD=<path>" in message


# ---------------------------------------------------------------------------
# The run-start pinning boundary: rc 2, stderr, run notes, no roots written
# ---------------------------------------------------------------------------


def _notes_db(tmp_path, run_id: str) -> str:
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, notes TEXT)")
        con.execute("INSERT INTO task_runs (id, notes) VALUES (?, ?)", (run_id, ""))
        con.commit()
    finally:
        con.close()
    return str(db)


def test_persist_run_roots_fails_the_run_closed(tmp_path, monkeypatch, capsys):
    kick_repo = _init_repo(tmp_path / "kickrepo")
    kickoff = _kickoff_in(kick_repo)
    run_dir = _write_profile(tmp_path / "run", kickoff_path=kickoff)
    missing = str(tmp_path / "no-such-target")
    run_id = "run-fail-closed-1"
    db = _notes_db(tmp_path, run_id)

    for var in ("MINI_ORK_HOME", "MINI_ORK_ROOT", "MINI_ORK_ENGINE_ROOT",
                "MINI_ORK_TARGET_REPO", "MO_ALLOW_FRAMEWORK_CWD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MO_TARGET_CWD", missing)
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)

    with pytest.raises(SystemExit) as excinfo:
        run_roots.persist_run_roots(run_dir)
    assert excinfo.value.code == 2

    captured = capsys.readouterr()
    assert "does not exist" in captured.err
    assert missing in captured.err

    # A refused pin records no roots — the run must not start on a guess.
    profile = json.loads((tmp_path / "run" / "run_profile.json").read_text())
    assert "roots" not in profile

    # The durable reason reaches task_runs.notes, which recovery/the UI read.
    con = sqlite3.connect(db)
    try:
        note = con.execute("SELECT notes FROM task_runs WHERE id = ?",
                           (run_id,)).fetchone()[0]
    finally:
        con.close()
    assert "run-start refused" in note
    assert missing in note


def test_persist_run_roots_is_a_no_op_for_a_missing_profile(tmp_path):
    assert run_roots.persist_run_roots(str(tmp_path / "absent")) is None
