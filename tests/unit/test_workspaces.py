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
    assert ws.path == home / "worktrees" / "run-1700-aabbcc"
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
