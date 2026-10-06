"""Real-subprocess tests for ``mini_ork.workspaces`` (Zed S4 IPC).

The kickoff specifies "real temp git repos — ``git init``, a commit; set
``user.name``/``user.email`` in the temp repo". We exercise the actual
``_git`` seam so ff-vs-no-ff and ``merge --abort`` on conflict both surface
as failures rather than mock-vs-real divergences. Test function names use
the ``test_ws_`` prefix so they never collide with the seven existing
``test_workspace_*`` files in this directory.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mini_ork import workspaces as ws_mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(tmp_path: Path, *, name: str = "proj") -> Path:
    """Make a fresh temp git repo with one commit, return its path."""
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


# ── create ──────────────────────────────────────────────────────────────────


def test_ws_create_mints_worktree_branch_record(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-aabbcc")
    assert ws.run_id == "run-1700-aabbcc"
    assert ws.branch == "mini-ork/run-1700-aabbcc"
    # Z-W1: the worktree lives under ``worktree_dir(project)`` (default
    # ``../worktrees/<project name>/<run id>``) — not ``<home>/``.
    expected_dir = ws_mod.worktree_dir(project)
    assert ws.path == expected_dir / "run-1700-aabbcc"
    assert ws.path.is_relative_to(expected_dir)
    assert ws.base_branch == "main"
    assert ws.path.is_dir()
    assert (ws.path / "README.md").read_text(encoding="utf-8") == "hi\n"
    record = home / "worktrees" / "run-1700-aabbcc.json"
    assert record.is_file()
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["branch"] == "mini-ork/run-1700-aabbcc"
    assert payload["base_branch"] == "main"
    # Worktree shows up in ``git worktree list``.
    listed = _git(["worktree", "list"], project).stdout
    assert "run-1700-aabbcc" in listed


def test_ws_create_runs_setup_hook_and_records_outcome(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    hook = home / "worktree-setup.sh"
    hook.write_text(
        "#!/usr/bin/env bash\nset -e\necho hook-ran >> setup.log\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    ws = ws_mod.create(project, home, "run-1700-112233")
    assert (ws.path / "setup.log").read_text(encoding="utf-8") == "hook-ran\n"
    record = json.loads((home / "worktrees" / "run-1700-112233.json").read_text())
    assert record["setup"]["ran"] is True
    assert record["setup"]["rc"] == 0


def test_ws_create_reports_setup_hook_failure_without_deleting_worktree(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    hook = home / "worktree-setup.sh"
    hook.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
    hook.chmod(0o755)
    ws = ws_mod.create(project, home, "run-1700-998877")
    assert ws.path.is_dir()
    record = json.loads((home / "worktrees" / "run-1700-998877.json").read_text())
    assert record["setup"]["ran"] is True
    assert record["setup"]["rc"] == 42


def test_ws_create_raises_when_project_is_not_a_git_repo(tmp_path):
    project = tmp_path / "not-a-repo"
    project.mkdir()
    home = _init_home(tmp_path)
    with pytest.raises(RuntimeError, match="not a git repo"):
        ws_mod.create(project, home, "run-1700-abcdef")


# ── status ──────────────────────────────────────────────────────────────────


def test_ws_status_reports_commit_and_uncommitted_edits(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-commits")
    # One commit ahead on the worktree branch.
    (ws.path / "new.txt").write_text("new\n", encoding="utf-8")
    assert _git(["add", "new.txt"], ws.path).returncode == 0
    assert _git(["commit", "-m", "add"], ws.path).returncode == 0
    # Uncommitted edit on top.
    (ws.path / "new.txt").write_text("newer\n", encoding="utf-8")
    snap = ws_mod.status(ws)
    assert snap["exists"] is True
    assert snap["commits_ahead"] == 1
    assert snap["uncommitted"]  # dirty porcelain
    assert snap["added"] >= 1
    assert any(f["path"] == "new.txt" for f in snap["files"])


def test_ws_status_missing_worktree(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-missing")
    shutil.rmtree(ws.path)
    snap = ws_mod.status(ws)
    assert snap == {"exists": False}


# ── merge: fast-forward ─────────────────────────────────────────────────────


def test_ws_merge_fast_forward(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-ff")
    (ws.path / "feature.txt").write_text("x\n", encoding="utf-8")
    assert _git(["add", "feature.txt"], ws.path).returncode == 0
    assert _git(["commit", "-m", "feat"], ws.path).returncode == 0
    result = ws_mod.merge(ws, "merge feat")
    assert result["ok"] is True
    assert result["mode"] == "fast-forward"
    assert (project / "feature.txt").is_file()
    # Workspace is gone after merge.
    assert ws_mod.load(home, ws.run_id) is None


def test_ws_merge_no_ff_when_base_advanced_after_create(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-noff")
    # Land a commit on the base branch AFTER the workspace was created.
    (project / "main-only.txt").write_text("m\n", encoding="utf-8")
    assert _git(["add", "main-only.txt"], project).returncode == 0
    assert _git(["commit", "-m", "main advance"], project).returncode == 0
    # And one on the worktree branch.
    (ws.path / "feature.txt").write_text("f\n", encoding="utf-8")
    assert _git(["add", "feature.txt"], ws.path).returncode == 0
    assert _git(["commit", "-m", "feat"], ws.path).returncode == 0
    result = ws_mod.merge(ws, "merge feat")
    assert result["ok"] is True
    assert result["mode"] == "merge"


def test_ws_merge_conflict_is_aborted_and_reported(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-conflict")
    # Both sides change the same lines.
    (project / "README.md").write_text("line A\n", encoding="utf-8")
    assert _git(["commit", "-am", "main edit"], project).returncode == 0
    (ws.path / "README.md").write_text("line B\n", encoding="utf-8")
    assert _git(["commit", "-am", "ws edit"], ws.path).returncode == 0
    result = ws_mod.merge(ws, "merge feat")
    assert result["ok"] is False
    assert "conflicts" in result["error"]
    # The aborted merge left the project cleanly on main, no README conflict markers.
    text = (project / "README.md").read_text(encoding="utf-8")
    assert "<<<<<<" not in text


def test_ws_merge_refused_when_project_dirty_overlap(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-dirty")
    (ws.path / "shared.txt").write_text("from ws\n", encoding="utf-8")
    assert _git(["add", "shared.txt"], ws.path).returncode == 0
    assert _git(["commit", "-m", "ws"], ws.path).returncode == 0
    # Project dirty on the SAME file.
    (project / "shared.txt").write_text("from main dirty\n", encoding="utf-8")
    assert _git(["add", "shared.txt"], project).returncode == 0
    result = ws_mod.merge(ws, "merge feat")
    assert result["ok"] is False
    assert "dirty overlapping" in result["error"]
    # Clean up so the next test starts clean.
    assert _git(["reset", "--hard", "HEAD"], project).returncode == 0


def test_ws_merge_refused_when_project_on_wrong_branch(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-branch")
    (ws.path / "feat.txt").write_text("f\n", encoding="utf-8")
    assert _git(["add", "feat.txt"], ws.path).returncode == 0
    assert _git(["commit", "-m", "feat"], ws.path).returncode == 0
    # Switch the project onto a different branch.
    assert _git(["checkout", "-b", "other"], project).returncode == 0
    try:
        result = ws_mod.merge(ws, "merge feat")
        assert result["ok"] is False
        assert "expected 'main'" in result["error"]
    finally:
        _git(["checkout", "main"], project)


def test_ws_merge_with_nothing_to_merge_discards(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-empty")
    result = ws_mod.merge(ws, "merge nothing")
    assert result == {"ok": True, "merged": None}
    assert ws_mod.load(home, ws.run_id) is None


# ── discard ─────────────────────────────────────────────────────────────────


def test_ws_discard_removes_worktree_branch_record(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-discard")
    assert ws.path.is_dir()
    assert ws_mod.discard(ws) == {"ok": True}
    assert not ws.path.is_dir()
    # Record stays at ``<home>/worktrees/<run>.json`` even after the
    # worktree directory (now under ``worktree_dir(project)``) is gone.
    assert not (home / "worktrees" / "run-1700-discard.json").exists()


# ── list_open ───────────────────────────────────────────────────────────────


def test_ws_list_open_empty_after_merge(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-1700-listmerge")
    (ws.path / "f.txt").write_text("x\n", encoding="utf-8")
    assert _git(["add", "f.txt"], ws.path).returncode == 0
    assert _git(["commit", "-m", "f"], ws.path).returncode == 0
    assert ws_mod.merge(ws, "merge")["ok"] is True
    assert ws_mod.list_open(home) == []


def test_ws_list_open_empty_after_discard(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws_mod.create(project, home, "run-1700-listdiscard")
    ws2 = ws_mod.create(project, home, "run-1700-listdiscard2")
    ws_mod.discard(ws2)
    remaining = ws_mod.list_open(home)
    assert [w.run_id for w in remaining] == ["run-1700-listdiscard"]


def test_ws_load_returns_none_for_missing_record(tmp_path):
    home = _init_home(tmp_path)
    assert ws_mod.load(home, "never-existed") is None

def test_merge_uses_the_repos_identity_and_refuses_a_non_ff_merge_over_local_edits(tmp_path):
    """Leftover changes are committed as the user (repo identity), and a merge
    that needs a merge commit is refused while the project has uncommitted
    edits to tracked files — a conflicted merge could not restore them."""
    import subprocess

    from mini_ork import workspaces as w

    proj = tmp_path / "proj"
    proj.mkdir()

    def git(*args, cwd=proj):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Ada")
    git("config", "user.email", "ada@example.com")
    (proj / "a.txt").write_text("a\n")
    (proj / "b.txt").write_text("b\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    home = proj / ".mini-ork"
    home.mkdir()
    ws = w.create(proj, home, "run-x")
    (ws.path / "a.txt").write_text("a changed\n")  # left uncommitted in the worktree
    (proj / "c.txt").write_text("c\n")
    git("add", "c.txt")
    git("commit", "-qm", "base moved on")           # forces a merge commit
    (proj / "b.txt").write_text("local edit\n")     # unrelated uncommitted edit
    out = w.merge(ws, "change a")
    assert out["ok"] is False and "uncommitted changes" in out["error"]
    assert git("log", "-1", "--format=%ae", "mini-ork/run-x") .strip() == "ada@example.com"
    assert (proj / "b.txt").read_text() == "local edit\n"   # untouched


def test_status_counts_files_the_run_created(tmp_path: Path) -> None:
    """A run that only adds files is a change: untracked files count."""
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-new-files")
    (ws.path / "notes.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (ws.path / "pkg").mkdir()
    (ws.path / "pkg" / "mod.py").write_text("x = 1", encoding="utf-8")  # no trailing newline
    st = ws_mod.status(ws)
    assert (st["added"], st["removed"]) == (4, 0)
    assert {f["path"] for f in st["files"]} == {"notes.md", "pkg/mod.py"}


# ── Z-W1: main_checkout / is_linked_worktree / worktree_dir / task_name / adopt


def _add_linked_worktree(project: Path, *, branch: str = "wt") -> Path:
    """Add a linked worktree at ``tmp_path/worktree/<branch>`` and return it."""
    wt_path = project.parent / f"{project.name}-wt"
    assert _git(["worktree", "add", "-b", branch, str(wt_path)], project).returncode == 0
    return wt_path


def test_main_checkout_returns_main_root_for_main(tmp_path):
    project = _init_repo(tmp_path)
    assert ws_mod.main_checkout(project) == project


def test_main_checkout_returns_main_root_for_linked(tmp_path):
    project = _init_repo(tmp_path)
    wt = _add_linked_worktree(project)
    assert ws_mod.main_checkout(wt) == project


def test_main_checkout_returns_none_for_non_repo(tmp_path):
    assert ws_mod.main_checkout(tmp_path / "nope") is None


def test_is_linked_worktree_false_for_main_and_true_for_linked(tmp_path):
    project = _init_repo(tmp_path)
    wt = _add_linked_worktree(project)
    assert ws_mod.is_linked_worktree(project) is False
    assert ws_mod.is_linked_worktree(wt) is True
    assert ws_mod.is_linked_worktree(tmp_path / "nope") is False


def test_worktree_dir_default_appends_project_name_when_outside(tmp_path, monkeypatch):
    """Default ``../worktrees`` lives outside the main checkout, so the
    project name is appended (Zed's rule)."""
    project = _init_repo(tmp_path)
    monkeypatch.delenv("MO_WORKTREE_DIR", raising=False)
    monkeypatch.delenv("ZED_SETTINGS", raising=False)
    wt = ws_mod.worktree_dir(project)
    assert wt.name == project.name  # appended
    assert ws_mod.worktree_dir(project).parent.name == "worktrees"


def test_worktree_dir_honors_mo_worktree_dir_env(tmp_path, monkeypatch):
    project = _init_repo(tmp_path)
    target = tmp_path / "wt"
    target.mkdir()
    monkeypatch.setenv("MO_WORKTREE_DIR", str(target))
    # The target lives OUTSIDE the main checkout → project name is appended
    # (Zed's rule, kickoff §worktree_dir).
    assert ws_mod.worktree_dir(project) == target / project.name


def test_worktree_dir_honors_project_zed_settings_jsonc(tmp_path, monkeypatch):
    """``<main>/.zed/settings.json`` with JSONC comments wins over the
    default; lenient parser strips them."""
    project = _init_repo(tmp_path)
    monkeypatch.delenv("MO_WORKTREE_DIR", raising=False)
    monkeypatch.delenv("ZED_SETTINGS", raising=False)
    zed_dir = project / ".zed"
    zed_dir.mkdir()
    custom = tmp_path / "custom-wts"
    custom.mkdir()
    # JSONC: comments + trailing comma must parse.
    (zed_dir / "settings.json").write_text(
        "// Zed user settings\n"
        "{\n"
        '  "git": { "worktree_directory": "' + str(custom) + '" },  // trailing\n'
        "}\n",
        encoding="utf-8",
    )
    # The custom dir is outside the main checkout → project name appended.
    assert ws_mod.worktree_dir(project) == custom / project.name


def test_create_uses_worktree_dir_and_records_setup(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    ws = ws_mod.create(project, home, "run-w1-create", name="my-task-abc123")
    expected_dir = ws_mod.worktree_dir(project)
    assert ws.path.parent == expected_dir
    assert ws.path.name == "my-task-abc123"
    # Record still under home/worktrees/.
    assert (home / "worktrees" / "run-w1-create.json").is_file()
    # Default ``create(..., run_id)`` (no name) uses run_id.
    ws2 = ws_mod.create(project, home, "run-w1-default")
    assert ws2.path.name == "run-w1-default"


def test_task_name_slug_and_suffix(tmp_path):
    # Lowercase, non-alnum → ``-``, collapsed, trimmed, capped at 40.
    title = "  Hello, World! Build & Test 2026  "
    rid = "run-1700-aabbcc"
    assert ws_mod.task_name(title, rid) == "hello-world-build-test-2026-aabbcc"
    # Empty title → just the last 6 of run_id.
    assert ws_mod.task_name("", rid) == "aabbcc"
    # Very long title is capped at 40.
    long = "x" * 100
    assert ws_mod.task_name(long, rid).startswith("x" * 40 + "-")
    assert ws_mod.task_name(long, rid).endswith("-aabbcc")


def test_adopt_on_named_branch_keeps_it_and_records_clean(tmp_path):
    project = _init_repo(tmp_path)
    wt = _add_linked_worktree(project, branch="wt-named")
    home = _init_home(tmp_path)
    ws = ws_mod.adopt(wt, home, "run-w1-adopt-1")
    assert ws.adopted is True
    assert ws.clean_at_start is True  # pristine worktree
    assert ws.branch == "wt-named"
    assert ws.project == project
    # The base_sha is HEAD now and base_branch is the main checkout's branch.
    head_sha = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    assert ws.base_sha == head_sha
    assert ws.base_branch == "main"
    # Record lives at home/worktrees/<run>.json.
    assert (home / "worktrees" / "run-w1-adopt-1.json").is_file()


def test_adopt_on_detached_head_creates_mini_ork_branch(tmp_path):
    project = _init_repo(tmp_path)
    wt = _add_linked_worktree(project, branch="wt-detach")
    # Detach HEAD in the linked worktree.
    assert _git(["checkout", "--detach", "HEAD"], wt).returncode == 0
    assert _git(["branch", "-D", "wt-detach"], wt).returncode == 0  # branch gone
    home = _init_home(tmp_path)
    ws = ws_mod.adopt(wt, home, "run-w1-adopt-detach")
    assert ws.branch == "mini-ork/run-w1-adopt-detach"
    assert _git(["branch", "--list", "mini-ork/run-w1-adopt-detach"], wt).returncode == 0


def test_adopt_clean_at_start_is_false_when_worktree_dirty(tmp_path):
    project = _init_repo(tmp_path)
    wt = _add_linked_worktree(project, branch="wt-dirty")
    (wt / "draft.txt").write_text("scratch\n", encoding="utf-8")
    home = _init_home(tmp_path)
    ws = ws_mod.adopt(wt, home, "run-w1-adopt-dirty")
    assert ws.clean_at_start is False


def test_adopt_raises_outside_a_linked_worktree(tmp_path):
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    with pytest.raises(RuntimeError, match="not a linked worktree"):
        ws_mod.adopt(project, home, "run-w1-adopt-no")


def test_merge_on_adopted_workspace_keeps_worktree_and_branch(tmp_path):
    project = _init_repo(tmp_path)
    wt = _add_linked_worktree(project, branch="wt-merge")
    home = _init_home(tmp_path)
    ws = ws_mod.adopt(wt, home, "run-w1-merge")
    # Land a commit on the worktree branch.
    (wt / "feature.txt").write_text("f\n", encoding="utf-8")
    assert _git(["add", "feature.txt"], wt).returncode == 0
    assert _git(["commit", "-m", "feat"], wt).returncode == 0
    result = ws_mod.merge(ws, "merge adopted")
    assert result["ok"] is True
    assert result["mode"] == "fast-forward"
    # Worktree is kept (Zed owns the worktree).
    assert wt.is_dir()
    # Branch is kept (Zed owns it; the branch is checked out there).
    assert "wt-merge" in _git(["branch", "--list"], project).stdout
    # Record was deleted.
    assert ws_mod.load(home, ws.run_id) is None
    # Project has the new file.
    assert (project / "feature.txt").is_file()


def test_discard_on_adopted_clean_workspace_resets_and_clears(tmp_path):
    project = _init_repo(tmp_path)
    wt = _add_linked_worktree(project, branch="wt-discard")
    home = _init_home(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    ws = ws_mod.adopt(wt, home, "run-w1-discard-clean")
    # A run commit on the worktree branch.
    (wt / "draft.txt").write_text("scratch\n", encoding="utf-8")
    assert _git(["add", "draft.txt"], wt).returncode == 0
    assert _git(["commit", "-m", "run"], wt).returncode == 0
    out = ws_mod.discard(ws)
    assert out == {"ok": True}
    # Reset to base_sha — draft.txt is gone.
    assert not (wt / "draft.txt").exists()
    head_after = _git(["rev-parse", "HEAD"], wt).stdout.strip()
    assert head_after == base_sha
    # Branch and worktree untouched.
    assert wt.is_dir()
    assert "wt-discard" in _git(["branch", "--list"], project).stdout
    # Record deleted.
    assert ws_mod.load(home, ws.run_id) is None


def test_discard_on_adopted_dirty_workspace_refuses(tmp_path):
    project = _init_repo(tmp_path)
    wt = _add_linked_worktree(project, branch="wt-dirty-discard")
    (wt / "pre.txt").write_text("pre-existing\n", encoding="utf-8")
    home = _init_home(tmp_path)
    ws = ws_mod.adopt(wt, home, "run-w1-discard-dirty")
    assert ws.clean_at_start is False
    out = ws_mod.discard(ws)
    assert out["ok"] is False
    assert "before the run" in out["error"]
    # Worktree is untouched; record is untouched.
    assert wt.is_dir()
    assert ws_mod.load(home, ws.run_id) is not None
    assert (wt / "pre.txt").exists()


def test_legacy_record_with_old_path_still_loads(tmp_path):
    """A record written before Z-W1 (path == ``<home>/worktrees/<run>``)
    still loads because ``Workspace.path`` stores whatever the record held.
    """
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    legacy_path = home / "worktrees" / "run-w1-legacy"
    legacy_path.mkdir(parents=True, exist_ok=True)
    record = home / "worktrees" / "run-w1-legacy.json"
    payload = {
        "run_id": "run-w1-legacy",
        "path": str(legacy_path),
        "branch": "mini-ork/run-w1-legacy",
        "base_branch": "main",
        "base_sha": "deadbeef",
        "project": str(project),
    }
    record.write_text(json.dumps(payload), encoding="utf-8")
    ws = ws_mod.load(home, "run-w1-legacy")
    assert ws is not None
    assert ws.path == legacy_path
    assert ws.adopted is False  # default
    assert ws.clean_at_start is False


def test_changed_paths_and_base_text(tmp_path: Path) -> None:
    project = _init_repo(tmp_path)
    home = _init_home(tmp_path)
    (project / "gone.txt").write_text("bye\n")
    assert _git(["add", "gone.txt"], project).returncode == 0
    assert _git(["commit", "-m", "gone"], project).returncode == 0
    ws = ws_mod.create(project, home, "run-changes")
    (ws.path / "README.md").write_text("hi\nmore\n")
    (ws.path / "fresh.md").write_text("new\n")
    (ws.path / "gone.txt").unlink()
    assert ws_mod.changed_paths(ws) == [("M", "README.md"), ("A", "fresh.md"), ("D", "gone.txt")]
    assert ws_mod.base_text(ws, "README.md") == "hi\n"
    assert ws_mod.base_text(ws, "fresh.md") is None
