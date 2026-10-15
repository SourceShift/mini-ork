"""Unit tests for ``mini_ork.vcs.node_checkpoint`` (per-node git refs).

Six DoD cases from the kickoff, each driving the real ``git`` binary
against a fresh temp repo. No network. ``monkeypatch`` resets any
leaked ``GIT_DIR`` / ``GIT_INDEX_FILE`` etc. that the surrounding
pytest run might have set.

The tests in :mod:`tests.unit.test_board_cmd` and
:mod:`tests.unit.test_checkpoint_py` keep passing unchanged — that is
DoD 7 and is asserted by the kickoff's verification command, not by a
test in this file.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.stores import migrate as mig
from mini_ork.vcs import node_checkpoint

REPO = Path(__file__).resolve().parents[2]


# ── shared helpers ──────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Real ``git`` invocation, mirroring the pattern in
    ``tests.unit.test_board_cmd`` so a future maintainer can move
    the helper without changing its call sites."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _git_out(args: list[str], cwd: Path) -> str:
    """Run ``git`` and return stdout stripped. Asserts zero exit so a
    failed helper call surfaces with a real error message, not a
    silent empty string further downstream."""
    r = _git(args, cwd)
    assert r.returncode == 0, (
        f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}"
    )
    return r.stdout.strip()


def _init_repo(tmp_path: Path, *, name: str = "proj") -> Path:
    """Fresh temp git repo with one commit. ``user.name``/``user.email``
    are set so ``commit-tree`` works without inheriting the outer
    machine's identity. ``commit.gpgsign=false`` is forced so a
    signing-enabled host still produces unsigned commits."""
    project = tmp_path / name
    project.mkdir()
    assert _git(["init", "-b", "main"], project).returncode == 0
    _git(["config", "user.name", "Test"], project)
    _git(["config", "user.email", "test@example.com"], project)
    _git(["config", "commit.gpgsign", "false"], project)
    _git(["config", "tag.gpgsign", "false"], project)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    assert _git(["add", "README.md"], project).returncode == 0
    assert _git(["commit", "-m", "init"], project).returncode == 0
    return project


def _strip_leaked_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the surrounding pytest or a fixture exported ``GIT_DIR`` /
    ``GIT_INDEX_FILE`` etc., the new snapshot's git subprocesses would
    be redirected away from the worktree. ``node_checkpoint._git`` does
    its own stripping, but doing it here too keeps the test readable
    when a regression test needs to introspect the env."""
    for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY", "GIT_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _clean_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_leaked_git_env(monkeypatch)


# ── DoD 1 — snapshot is invisible to git state ─────────────────────────────


def test_snapshot_does_not_touch_head_index_or_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot of a worktree with one tracked edit and one new
    untracked file returns a sha, the ref exists, and the real
    ``git status``/HEAD/index are unchanged. The captured tree
    contains the untracked file and excludes a ``.gitignore``d file."""
    project = _init_repo(tmp_path)
    # Tracked edit
    (project / "README.md").write_text("hi v2\n", encoding="utf-8")
    # New untracked file
    (project / "scratch.txt").write_text("scratch\n", encoding="utf-8")
    # .gitignore'd file
    (project / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (project / "ignored.txt").write_text("never snapshotted\n", encoding="utf-8")
    status_before = _git(["status", "--porcelain"], project).stdout
    head_before = _git_out(["rev-parse", "HEAD"], project)
    diff_cached_before = _git(["diff", "--cached", "--quiet"], project).returncode

    sha = node_checkpoint.snapshot(project, "run-1", "implementer")
    assert sha, "snapshot must return a sha"

    # The ref exists and points at sha
    ref = "refs/mini-ork/ckpt/run-1/implementer"
    assert _git_out(["rev-parse", "--verify", ref], project) == sha

    # The user's git state is unchanged
    assert _git(["status", "--porcelain"], project).stdout == status_before
    assert _git_out(["rev-parse", "HEAD"], project) == head_before
    assert _git(["diff", "--cached", "--quiet"], project).returncode == diff_cached_before

    # The captured tree has the tracked edit, the untracked file, but
    # not the .gitignore'd file.
    tree = _git_out(["rev-parse", f"{sha}^{{tree}}"], project)
    tree_files = set(
        _git_out(["ls-tree", "-r", "--name-only", tree], project).splitlines()
    )
    assert "README.md" in tree_files
    assert "scratch.txt" in tree_files
    assert "ignored.txt" not in tree_files


def test_snapshot_on_non_git_dir_returns_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare directory (no ``.git``) yields ``None`` and a stderr line,
    never raises — the kickoff's DoD 5."""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert node_checkpoint.snapshot(bare, "run-1", "implementer") is None
    err = capsys.readouterr().err
    assert "[ckpt]" in err


# ── DoD 2 — overwrite=False + sanitised node id ────────────────────────────


def test_overwrite_false_keeps_first_sha(tmp_path: Path) -> None:
    """The first snapshot's sha is the permanent one — a second
    ``overwrite=False`` returns the same sha even after the worktree
    changes."""
    project = _init_repo(tmp_path)
    first = node_checkpoint.snapshot(project, "run-1", "implementer")
    assert first
    (project / "README.md").write_text("hi v2\n", encoding="utf-8")
    second = node_checkpoint.snapshot(project, "run-1", "implementer", overwrite=False)
    assert second == first
    third = node_checkpoint.snapshot(project, "run-1", "implementer", overwrite=True)
    assert third != first
    # The third ref is the latest; the ref still resolves to the new sha.
    ref = "refs/mini-ork/ckpt/run-1/implementer"
    assert _git_out(["rev-parse", "--verify", ref], project) == third


def test_sanitised_node_id_makes_a_valid_ref(tmp_path: Path) -> None:
    """A node id with a space and a slash is sanitised to a ref
    component that ``git check-ref-format`` accepts. The round-tripped
    ref resolves to the snapshot's sha."""
    project = _init_repo(tmp_path)
    sha = node_checkpoint.snapshot(project, "run-1", "a b/c")
    assert sha
    sanitised = node_checkpoint._sanitize("a b/c")
    # The character class substitution, then the additional
    # ``git check-ref-format`` rules the kickoff names: a leading
    # ``.``, a ``..``, a ``.lock`` suffix, a trailing ``.``, or an
    # empty string. None of those apply here, so the class pass is
    # the whole story.
    assert sanitised == "a_b_c"
    # The ref's structure is the contract; a stricter check would
    # call ``git check-ref-format`` on the full ref, but the ref was
    # just written by ``update-ref``, so its existence is itself the
    # acceptance proof.
    assert _git_out(["rev-parse", "--verify",
                     f"refs/mini-ork/ckpt/run-1/{sanitised}"], project) == sha


# ── DoD 3 — restore round-trip ──────────────────────────────────────────────


def test_restore_round_trip_through_safety_ref(tmp_path: Path) -> None:
    """Take a snapshot, mutate the worktree, force-restore, and the
    tree is back. The safety ref is recorded. Restoring to IT brings
    the mutated state back."""
    project = _init_repo(tmp_path)
    # Make a second tracked file
    (project / "second.txt").write_text("second\n", encoding="utf-8")
    _git(["add", "second.txt"], project)
    assert _git(["commit", "-m", "second"], project).returncode == 0

    # Snapshot the pristine state
    pre_sha = node_checkpoint.snapshot(project, "run-1", "implementer")
    assert pre_sha

    # Mutate: edit, delete, and create-untracked
    (project / "README.md").write_text("hi v2\n", encoding="utf-8")
    (project / "second.txt").unlink()
    (project / "new_thing.txt").write_text("fresh\n", encoding="utf-8")

    # The worktree now differs from the snapshot
    pre_tree = _git_out(["rev-parse", f"{pre_sha}^{{tree}}"], project)
    cur_tree = node_checkpoint._worktree_tree(project)
    assert cur_tree != pre_tree

    # Force-restore — the test repo is not a registered workspace,
    # so the safety refusal for "not the run's workspace" applies
    # without ``force=True``.
    head_before = _git_out(["rev-parse", "HEAD"], project)
    result = node_checkpoint.restore(project, "run-1", "implementer", force=True)
    assert result["ok"] is True, result
    assert result["restored_to"] == pre_sha
    safety_ref = result["safety_ref"]
    assert safety_ref and safety_ref.startswith("refs/mini-ork/ckpt/run-1/_pre-restore-")

    # The worktree equals the snapshot
    assert node_checkpoint._worktree_tree(project) == pre_tree
    assert (project / "README.md").read_text(encoding="utf-8") == "hi\n"
    assert (project / "second.txt").is_file()
    assert not (project / "new_thing.txt").exists()
    # HEAD is unchanged (DoD 3): captured before restore() so the
    # comparison actually checks something.
    assert _git_out(["rev-parse", "HEAD"], project) == head_before
    # Index is at HEAD (so checkpoint untracked files come back untracked)
    assert _git(["diff", "--cached", "--quiet"], project).returncode == 0

    # Restoring to the safety ref brings the mutated state back.
    safety_name = safety_ref.rsplit("/", 1)[-1]
    result2 = node_checkpoint.restore(project, "run-1", safety_name, force=True)
    assert result2["ok"] is True, result2
    assert (project / "README.md").read_text(encoding="utf-8") == "hi v2\n"
    assert not (project / "second.txt").exists()
    assert (project / "new_thing.txt").is_file()


# ── DoD 4 — restore refusals ───────────────────────────────────────────────


def test_restore_unknown_node_refuses(tmp_path: Path) -> None:
    """An unknown node id returns ``ok: False`` and the worktree is
    untouched."""
    project = _init_repo(tmp_path)
    (project / "README.md").write_text("untouched\n", encoding="utf-8")
    status_before = _git(["status", "--porcelain"], project).stdout
    result = node_checkpoint.restore(project, "run-1", "ghost", force=True)
    assert result["ok"] is False
    assert "unknown" in result["reason"]
    assert _git(["status", "--porcelain"], project).stdout == status_before


def test_restore_workspace_mismatch_refuses_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worktree that is not the run's own workspace refuses without
    ``force=True``. The worktree is not modified."""
    # Real worktree with a snapshot
    project = _init_repo(tmp_path, name="wt")
    sha = node_checkpoint.snapshot(project, "run-1", "implementer")
    assert sha
    # A different home that points at a DIFFERENT path for this run
    home = tmp_path / "home"
    home.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    fake_record = home / "worktrees" / "run-1.json"
    fake_record.parent.mkdir(parents=True, exist_ok=True)
    fake_record.write_text(json.dumps({
        "run_id": "run-1",
        "path": str(other),
        "branch": "mini-ork/run-1",
        "base_branch": "main",
        "base_sha": "0" * 40,
        "project": str(other),
        "adopted": True,
        "clean_at_start": True,
        "home": str(home),
    }))

    status_before = _git(["status", "--porcelain"], project).stdout
    result = node_checkpoint.restore(project, "run-1", "implementer", home=home)
    assert result["ok"] is False
    assert "workspace" in result["reason"]
    # No file changes, no safety ref created
    assert _git(["status", "--porcelain"], project).stdout == status_before
    list_out = _git_out(["for-each-ref", "refs/mini-ork/ckpt/run-1/"], project)
    assert "_pre-restore-" not in list_out

    # With force=True the workspace check is bypassed and the
    # restore proceeds; this is the contract the kickoff names.
    forced = node_checkpoint.restore(
        project, "run-1", "implementer", home=home, force=True,
    )
    assert forced["ok"] is True, forced


def test_restore_live_run_refuses_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose ``task_runs.status`` is ``executing`` refuses
    without ``force=True``. A run in ``reviewing`` is allowed."""
    # Real worktree with a snapshot
    project = _init_repo(tmp_path, name="wt")
    sha = node_checkpoint.snapshot(project, "run-1", "implementer")
    assert sha

    # Home with state.db and a task_runs row
    home = tmp_path / "home"
    rc, _out, _err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, "
        "updated_at, task_class, kickoff_path, workflow_version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("run-1", "framework-edit", "executing", 0.0, now, now,
         "framework_edit", "", "latest"),
    )
    con.commit()
    con.close()

    # Register the worktree as the run's workspace, so the workspace
    # check passes and only the live-run check fires.
    (home / "worktrees" / "run-1.json").parent.mkdir(parents=True, exist_ok=True)
    (home / "worktrees" / "run-1.json").write_text(json.dumps({
        "run_id": "run-1",
        "path": str(project),
        "branch": "mini-ork/run-1",
        "base_branch": "main",
        "base_sha": "0" * 40,
        "project": str(project),
        "adopted": True,
        "clean_at_start": True,
        "home": str(home),
    }))

    status_before = _git(["status", "--porcelain"], project).stdout
    result = node_checkpoint.restore(project, "run-1", "implementer", home=home)
    assert result["ok"] is False
    assert "live" in result["reason"]
    assert _git(["status", "--porcelain"], project).stdout == status_before

    # reviewing is the explicit exception: a human-paused run is
    # exactly when a restore is wanted.
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET status = 'reviewing' WHERE id = 'run-1'")
    con.commit()
    con.close()
    reviewing = node_checkpoint.restore(project, "run-1", "implementer", home=home)
    assert reviewing["ok"] is True, reviewing


def test_restore_without_home_or_force_fails_closed(
    tmp_path: Path,
) -> None:
    """A direct library caller that omits ``home`` must not get an
    unforced restore. With ``home=None`` there is no workspace record
    to prove the worktree belongs to the run, so the call fails closed
    with ``ok: False, reason: "not the run's workspace"``. With
    ``force=True`` the same call succeeds — the contract is opt-in."""
    project = _init_repo(tmp_path, name="wt")
    sha = node_checkpoint.snapshot(project, "run-1", "implementer")
    assert sha
    status_before = _git(["status", "--porcelain"], project).stdout

    # No home, no force → fail closed.
    result = node_checkpoint.restore(project, "run-1", "implementer")
    assert result["ok"] is False
    assert "workspace" in result["reason"]
    # No safety ref was created, no files touched.
    assert _git(["status", "--porcelain"], project).stdout == status_before
    list_out = _git_out(["for-each-ref", "refs/mini-ork/ckpt/run-1/"], project)
    assert "_pre-restore-" not in list_out

    # Opting in with force=True proceeds.
    forced = node_checkpoint.restore(project, "run-1", "implementer", force=True)
    assert forced["ok"] is True, forced


# ── DoD 6 — board verbs via main() ─────────────────────────────────────────


def test_board_checkpoints_verb_lists_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """``board checkpoints <run>`` runs end-to-end through ``main()``
    and returns a payload with the per-node refs."""
    project = _init_repo(tmp_path, name="wt")
    node_checkpoint.snapshot(project, "run-board-1", "implementer")
    node_checkpoint.snapshot(project, "run-board-1", "reviewer")

    # Home is a separate ``.mini-ork``; no state.db needed for this verb
    home = tmp_path / "home"
    home.mkdir()
    (home / "worktrees" / "run-board-1.json").parent.mkdir(parents=True, exist_ok=True)
    (home / "worktrees" / "run-board-1.json").write_text(json.dumps({
        "run_id": "run-board-1",
        "path": str(project),
        "branch": "mini-ork/run-board-1",
        "base_branch": "main",
        "base_sha": "0" * 40,
        "project": str(project),
        "adopted": True,
        "clean_at_start": True,
        "home": str(home),
    }))

    rc = board_cmd.main(["checkpoints", "run-board-1", "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["run_id"] == "run-board-1"
    assert {c["node"] for c in payload["checkpoints"]} == {"implementer", "reviewer"}


def test_board_restore_verb_force_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """``board restore <run> <node> --force`` runs end-to-end through
    ``main()`` and exits 0. ``ok: False`` exits 1 with the reason on
    stderr."""
    project = _init_repo(tmp_path, name="wt")
    node_checkpoint.snapshot(project, "run-board-2", "implementer")
    (project / "README.md").write_text("mutated\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    # Register the worktree as the run's workspace so ``main()``'s
    # ``_resolve_worktree_for_run`` finds it. With ``--force`` the
    # restore's own workspace check is also bypassed, but the board
    # helper needs the path.
    (home / "worktrees" / "run-board-2.json").parent.mkdir(parents=True, exist_ok=True)
    (home / "worktrees" / "run-board-2.json").write_text(json.dumps({
        "run_id": "run-board-2",
        "path": str(project),
        "branch": "mini-ork/run-board-2",
        "base_branch": "main",
        "base_sha": "0" * 40,
        "project": str(project),
        "adopted": True,
        "clean_at_start": True,
        "home": str(home),
    }))

    rc = board_cmd.main(
        ["restore", "run-board-2", "implementer", "--home", str(home), "--force"], "",
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["restored_to"]
    assert payload["safety_ref"].startswith("refs/mini-ork/ckpt/run-board-2/_pre-restore-")
    # The README is back to the original content
    assert (project / "README.md").read_text(encoding="utf-8") == "hi\n"

    # And a refusal surfaces a non-zero exit + stderr reason.
    rc = board_cmd.main(
        ["restore", "run-board-2", "ghost", "--home", str(home), "--force"], "",
    )
    assert rc == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "unknown" in payload["reason"]
    assert "unknown" in captured.err


def test_board_restore_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing run id / missing node id both exit 2 with a usage
    message on stderr."""
    home = tmp_path / "home"
    home.mkdir()
    assert board_cmd.main(["restore", "--home", str(home)], "") == 2
    assert board_cmd.main(["restore", "run-x", "--home", str(home)], "") == 2
    assert "run id" in capsys.readouterr().err


# ── execute.py hooks — run_dir → resolved target, MO_NODE_GIT_CKPT gate ─────


def test_make_checkpoint_fn_snapshots_resolved_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_make_checkpoint_fn`` must snapshot the run's resolved target
    worktree (the tree the implementer edits), NOT the run directory.
    The kickoff's run dir is ``.mini-ork/runs/<id>`` — git-excluded in
    the engine checkout — so a closure that captured ``run_dir`` would
    silently snapshot the engine repo's HEAD tree or fall through with
    ``[ckpt] not a git work tree``. The ref must land in the target
    repo, carrying the post-success tree (including the new edit)."""
    from mini_ork.cli import execute as ex

    # The target worktree the node just edited — a real git repo with
    # one tracked file and a pending edit.
    target = _init_repo(tmp_path, name="target")
    (target / "README.md").write_text("post-edit\n", encoding="utf-8")

    # A SEPARATE repo standing in for the engine checkout. The previous
    # bug wrote the ref here (because run_dir used to be passed in);
    # this test must NOT see a ref in this repo.
    engine = _init_repo(tmp_path, name="engine")
    (engine / "engine-marker.txt").write_text("engine-only\n", encoding="utf-8")

    # run_dir is ``.mini-ork/runs/<id>`` — point its run_profile.json at
    # the target so ``_resolve_target_cwd`` resolves there.
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "kickoff_path": str(target / "kickoff.md"),
        "roots": {"target": str(target), "run_dir": str(run_dir),
                   "home": str(tmp_path / "home"),
                   "engine": str(engine), "exec_cwd": None},
    }), encoding="utf-8")
    # The kickoff file must exist for the parity-resolve ladder, even
    # though the persisted ``roots.target`` is the authoritative one.
    (target / "kickoff.md").write_text("k\n", encoding="utf-8")

    # Capture the expected tree AFTER all setup is done — the snapshot
    # will see kickoff.md (it is on disk by then), so the comparison
    # must include it.
    target_tree_before = node_checkpoint._worktree_tree(target)

    monkeypatch.setenv("MO_TARGET_CWD", "")
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_NODE_GIT_CKPT", raising=False)

    # Stub the DB write so the closure proceeds past the
    # ``if ckpt_rc == 0`` gate — the test asserts on the git ref, not
    # the DB row, and standing up a real SQLite schema for this would
    # double the fixture cost for no extra coverage.
    from mini_ork.stores import checkpoints as mc
    monkeypatch.setattr(mc, "write_checkpoint",
                        lambda *a, **kw: 0)

    db = str(tmp_path / "state.db")
    _cp = ex._make_checkpoint_fn(db, "run-hook-1", str(run_dir),
                                  recipe="framework-edit",
                                  task_class="framework_edit")
    # Drive the closure as if a node just succeeded.
    _cp("implementer", "success", "implementer", str(tmp_path / "out.txt"))

    # The ref lands in the TARGET repo, with the post-success tree.
    sha = _git_out(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-hook-1/implementer"],
        target,
    )
    assert sha
    assert _git_out(["rev-parse", f"{sha}^{{tree}}"], target) == target_tree_before
    # The new edit is in the captured tree.
    captured_files = set(
        _git_out(["ls-tree", "-r", "--name-only", f"{sha}^{{tree}}"], target).splitlines()
    )
    assert "README.md" in captured_files

    # And the engine repo (which is what ``run_dir`` would have
    # resolved to before this fix) has NO ref for this run.
    assert _git(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-hook-1/implementer"],
        engine,
    ).returncode != 0


def test_make_checkpoint_fn_respects_mo_node_git_ckpt_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MO_NODE_GIT_CKPT=0`` skips the snapshot. ``_make_checkpoint_fn``
    returns a closure; the closure must call ``_node_git_ckpt`` with the
    gate in effect and produce NO ref."""
    from mini_ork.cli import execute as ex

    target = _init_repo(tmp_path, name="target")
    (target / "README.md").write_text("post-edit\n", encoding="utf-8")

    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "kickoff_path": str(target / "kickoff.md"),
        "roots": {"target": str(target), "run_dir": str(run_dir),
                   "home": str(tmp_path / "home"),
                   "engine": str(target), "exec_cwd": None},
    }), encoding="utf-8")
    (target / "kickoff.md").write_text("k\n", encoding="utf-8")

    monkeypatch.setenv("MO_NODE_GIT_CKPT", "0")

    from mini_ork.stores import checkpoints as mc
    monkeypatch.setattr(mc, "write_checkpoint",
                        lambda *a, **kw: 0)

    db = str(tmp_path / "state.db")
    _cp = ex._make_checkpoint_fn(db, "run-hook-2", str(run_dir),
                                  recipe="framework-edit",
                                  task_class="framework_edit")
    _cp("implementer", "success", "implementer", str(tmp_path / "out.txt"))

    # No ref was created.
    assert _git(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-hook-2/implementer"],
        target,
    ).returncode != 0


def test_main_start_hook_snapshots_resolved_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()``'s ``_start`` ref also lands in the resolved target,
    not the run directory. Both call sites must share the same ladder."""
    from mini_ork.cli import execute as ex

    target = _init_repo(tmp_path, name="target")

    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "kickoff_path": str(target / "kickoff.md"),
        "roots": {"target": str(target), "run_dir": str(run_dir),
                   "home": str(tmp_path / "home"),
                   "engine": str(target), "exec_cwd": None},
    }), encoding="utf-8")
    (target / "kickoff.md").write_text("k\n", encoding="utf-8")

    monkeypatch.setenv("MO_TARGET_CWD", "")
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_NODE_GIT_CKPT", raising=False)

    # Drive just the hook block from main() — full main() requires
    # DB + recipe machinery the unit test should not pay for.
    # ``_node_git_ckpt`` now resolves the target internally from
    # ``run_dir`` (kickoff hr-node-ckpt-refs round 2), so the call
    # site passes ``run_dir`` not a pre-resolved path.
    ex._node_git_ckpt(str(run_dir), "run-start-1", "_start", overwrite=False)

    # The _start ref landed in the target repo.
    sha = _git_out(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-start-1/_start"],
        target,
    )
    assert sha


# ── round-2 HIGH bug: kickoff in a different repo than roots.target ─────────
#
# Before the round-2 fix, ``_node_git_ckpt`` resolved its snapshot
# target with ``_resolve_target_cwd(run_dir)`` (= ``resolve_run_roots``).
# That ladder prefers the kickoff's git toplevel over the persisted
# ``roots.target``, so a run resumed with ``MO_TARGET_CWD`` unset
# (the ``mini-ork resume <run>`` path) would snapshot whatever repo
# the kickoff lives in, NOT the implementer's worktree — and on a
# linked worktree (shared refs), the corruption was visible to every
# other worktree that queried the run. The fix mirrors the ladder at
# ``execute.py:2207`` — the persisted record wins, MO_TARGET_CWD is
# the fallback, and the cwd is the last resort.


def test_node_git_ckpt_prefers_persisted_roots_over_kickoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the persisted ``roots.target`` and the kickoff's git
    toplevel point at different repos, ``_node_git_ckpt`` writes the
    ref in the PERSISTED target (the worktree the implementer edits),
    not in the kickoff's repo. The kickoff's repo gets no ref at all.

    ``MO_TARGET_CWD`` is unset so the test exercises the resume path
    (the bug only reproduced there). With the kickoff ladder both
    repos would have ended up with a ref, or the wrong one would have."""
    from mini_ork.cli import execute as ex

    # The implementer's actual worktree — what the run profile claims.
    target = _init_repo(tmp_path, name="target")
    (target / "README.md").write_text("post-edit\n", encoding="utf-8")
    (target / "feature.py").write_text("FEATURE\n", encoding="utf-8")

    # A separate repo that happens to host the kickoff file. The
    # previous bug wrote the ref here (because the kickoff's git
    # toplevel was preferred); this repo must end up empty of refs.
    kickoff_repo = _init_repo(tmp_path, name="kickoff_repo")
    kickoff_path = kickoff_repo / "k.md"
    kickoff_path.write_text("k\n", encoding="utf-8")

    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "kickoff_path": str(kickoff_path),
        "roots": {"target": str(target), "run_dir": str(run_dir),
                   "home": str(tmp_path / "home"),
                   "engine": str(kickoff_repo), "exec_cwd": None},
    }), encoding="utf-8")

    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_NODE_GIT_CKPT", raising=False)

    ex._node_git_ckpt(str(run_dir), "run-kickoff-vs-target", "implementer")

    # The ref is in the TARGET, not the kickoff's repo.
    target_sha = _git_out(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-kickoff-vs-target/implementer"],
        target,
    )
    assert target_sha, "ref must live in the persisted roots.target"
    captured = set(
        _git_out(
            ["ls-tree", "-r", "--name-only", f"{target_sha}^{{tree}}"],
            target,
        ).splitlines()
    )
    assert "feature.py" in captured
    assert "README.md" in captured

    # The kickoff's repo has NO ref for this run. A linked worktree
    # that shares refs would otherwise see the wrong ref here.
    assert _git(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-kickoff-vs-target/implementer"],
        kickoff_repo,
    ).returncode != 0


def test_node_git_ckpt_gate_runs_before_target_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 HIGH bug: the ``MO_NODE_GIT_CKPT`` gate MUST short-circuit
    BEFORE any target resolution runs. With ``resolve_run_roots`` (the
    round-1 implementation) the gate was checked AFTER target
    resolution, so a bad ``MO_SHARED_DRIVE_BACKEND`` propagated a
    ``ValueError`` from ``drive.up()`` even with the feature
    disabled — aborting ``main()`` before ``set_status('executing')``.
    The round-2 fix moves resolution INSIDE the wrapper, AFTER the
    gate. This test pins that ordering by stubbing the target
    resolution to raise; with the gate OFF the raise is never
    triggered (the wrapper returns early), and ``main()`` never sees
    the exception."""
    from mini_ork import runtime as _rt
    from mini_ork.cli import execute as ex

    def _boom(*a, **kw):
        raise ValueError("simulated bad drive backend (round-2 regression)")

    monkeypatch.setattr(_rt.run_roots, "load_run_roots", _boom)

    target = _init_repo(tmp_path, name="target")
    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "kickoff_path": str(target / "k.md"),
        "roots": {"target": str(target), "run_dir": str(run_dir),
                   "home": str(tmp_path / "home"),
                   "engine": str(target), "exec_cwd": None},
    }), encoding="utf-8")

    # Gate is OFF → the wrapper must return BEFORE reaching
    # ``load_run_roots``. No exception escapes, no ref is written.
    monkeypatch.setenv("MO_NODE_GIT_CKPT", "0")

    ex._node_git_ckpt(str(run_dir), "run-gate-order", "implementer")

    assert _git(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-gate-order/implementer"],
        target,
    ).returncode != 0

    # Gate is ON → the resolution runs, the stub raises, the wrapper
    # swallows the exception, and ``main()`` is still safe. The test
    # does not assert on the ref (the snapshot was skipped), only on
    # the absence of an escaped exception.
    monkeypatch.setenv("MO_NODE_GIT_CKPT", "1")
    ex._node_git_ckpt(str(run_dir), "run-gate-order", "implementer")


def test_make_checkpoint_fn_uses_run_dir_for_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_make_checkpoint_fn`` must not pre-resolve the snapshot
    target — passing ``run_dir`` to ``_node_git_ckpt`` keeps the
    resolution inside the wrapper, where the round-2 fix lives. This
    is the per-node companion to
    ``test_node_git_ckpt_prefers_persisted_roots_over_kickoff``."""
    from mini_ork.cli import execute as ex
    from mini_ork.stores import checkpoints as mc

    target = _init_repo(tmp_path, name="target")
    kickoff_repo = _init_repo(tmp_path, name="kickoff_repo")
    kickoff_path = kickoff_repo / "k.md"
    kickoff_path.write_text("k\n", encoding="utf-8")
    (target / "feature.py").write_text("FEATURE\n", encoding="utf-8")

    run_dir = tmp_path / "run_dir"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "kickoff_path": str(kickoff_path),
        "roots": {"target": str(target), "run_dir": str(run_dir),
                   "home": str(tmp_path / "home"),
                   "engine": str(kickoff_repo), "exec_cwd": None},
    }), encoding="utf-8")

    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    monkeypatch.delenv("MO_NODE_GIT_CKPT", raising=False)
    monkeypatch.setattr(mc, "write_checkpoint",
                        lambda *a, **kw: 0)

    db = str(tmp_path / "state.db")
    _cp = ex._make_checkpoint_fn(db, "run-closure", str(run_dir),
                                  recipe="framework-edit",
                                  task_class="framework_edit")
    _cp("implementer", "success", "implementer", str(tmp_path / "out.txt"))

    # Ref in the target, not in the kickoff's repo.
    assert _git_out(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-closure/implementer"],
        target,
    )
    assert _git(
        ["rev-parse", "--verify",
         "refs/mini-ork/ckpt/run-closure/implementer"],
        kickoff_repo,
    ).returncode != 0
