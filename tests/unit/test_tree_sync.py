"""Unit tests for ``mini_ork.remote.tree_sync`` (remote-nodes-07).

Real git, two repos, no HTTP: a LOCAL checkout (the truth) and a REMOTE replica
built from it with ``materialize``. Every flow below is the production flow —
snapshot, bundle, fetch, apply — not a one-repo shortcut.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mini_ork.remote import tree_sync as ts


def _git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _status(repo) -> set[str]:
    return set(_git(repo, "status", "--porcelain", "--untracked-files=all").splitlines())


def _user_state(repo) -> dict:
    return {"head": _git(repo, "rev-parse", "HEAD"), "index": _git(repo, "write-tree"),
            "stash": _git(repo, "stash", "list")}


@pytest.fixture
def local(tmp_path) -> str:
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    (repo / "keep.txt").write_text("keep\n")
    (repo / "gone.txt").write_text("to be deleted remotely\n")
    (repo / ".gitignore").write_text("*.log\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return str(repo)


def _materialize(local: str, remote: str):
    snap = ts.snapshot(local)
    bundle, mode = ts.initial_bundle(local, snap)
    try:
        tip = ts.bundle_tip(local)
        head = tip if mode == "squashed" else _git(local, "rev-parse", "HEAD")
        ts.materialize(remote, bundle, ts.Snap(tip, snap.tree, snap.excluded), head)
    finally:
        bundle.unlink(missing_ok=True)
    return ts.Snap(tip, snap.tree, snap.excluded)


def _sync_down(local: str, remote: str, base: ts.Snap, ref: str = "refs/mo/remote/t/latest"):
    new = ts.snapshot(remote)
    bundle = ts.incremental_bundle(remote, new=new.commit, base=base.commit)
    try:
        ts.fetch_bundle(local, bundle, ref)
    finally:
        bundle.unlink(missing_ok=True)
    return new, ts.apply_delta(local, base, new, ref=ref)


# --------------------------------------------------------------------------- snapshot


def test_snapshot_captures_worktree_without_touching_user_state(local):
    Path(local, "README.md").write_text("modified\n")
    Path(local, "new.txt").write_text("untracked\n")
    Path(local, "debug.log").write_text("ignored\n")
    Path(local, ".env").write_text("SECRET=shhh\n")
    _git(local, "stash", "push", "-q", "--include-untracked", "--", "new.txt")
    Path(local, "new.txt").write_text("untracked again\n")
    before = _user_state(local)
    snap = ts.snapshot(local)
    files = _git(local, "ls-tree", "-r", "--name-only", snap.tree).split()
    assert {"README.md", "new.txt", "keep.txt"} <= set(files)
    assert ".env" not in files and "debug.log" not in files
    assert snap.excluded == (".env",)
    assert _user_state(local) == before


def test_excluded_report_is_names_only(local):
    Path(local, ".env").write_text("ULTRA_SECRET_TOKEN=xxx")
    Path(local, "deploy.pem").write_text("-----BEGIN KEY-----")
    snap = ts.snapshot(local)
    assert set(snap.excluded) == {".env", "deploy.pem"}
    assert not any("ULTRA_SECRET" in n or "BEGIN" in n for n in snap.excluded)


def test_tracked_secret_keeps_its_committed_version(local):
    Path(local, ".env").write_text("COMMITTED=1\n")
    _git(local, "add", "-f", ".env")
    _git(local, "commit", "-q", "-m", "env")
    Path(local, ".env").write_text("LOCAL_SECRET=2\n")
    snap = ts.snapshot(local)
    assert _git(local, "show", f"{snap.tree}:.env") == "COMMITTED=1"


def test_extra_excludes_from_env(local, monkeypatch):
    monkeypatch.setenv("MO_REMOTE_SYNC_EXCLUDE", "*.sqlite")
    Path(local, "data.sqlite").write_text("x")
    assert "data.sqlite" not in _git(local, "ls-tree", "-r", "--name-only", ts.snapshot(local).tree)


# --------------------------------------------------------------------------- replica


def test_materialize_mirrors_local_status(local, tmp_path):
    Path(local, "README.md").write_text("modified\n")
    Path(local, "new.txt").write_text("untracked\n")
    Path(local, "debug.log").write_text("ignored\n")
    Path(local, ".env").write_text("SECRET=x\n")
    remote = str(tmp_path / "remote")
    _materialize(local, remote)
    assert _git(remote, "rev-parse", "HEAD") == _git(local, "rev-parse", "HEAD")
    expected = {ln for ln in _status(local) if not ln.endswith((".env", ".log"))}
    assert _status(remote) == expected
    assert not Path(remote, ".env").exists() and not Path(remote, "debug.log").exists()


def test_materialize_removes_stray_replica_files(local, tmp_path):
    remote = str(tmp_path / "remote")
    _materialize(local, remote)
    Path(remote, "stray.txt").write_text("left behind\n")
    _materialize(local, remote)  # re-materialize onto a dirty replica
    assert not Path(remote, "stray.txt").exists()


# --------------------------------------------------------------------------- delta


def test_remote_edits_land_locally_as_uncommitted(local, tmp_path):
    remote = str(tmp_path / "remote")
    base = _materialize(local, remote)
    before = _user_state(local)
    Path(remote, "README.md").write_text("edited remotely\n")
    Path(remote, "created.txt").write_text("new remote file\n")
    Path(remote, "gone.txt").unlink()
    new, head_moved = _sync_down(local, remote, base)
    assert head_moved is False
    assert Path(local, "README.md").read_text() == "edited remotely\n"
    assert Path(local, "created.txt").read_text() == "new remote file\n"
    assert not Path(local, "gone.txt").exists()
    assert ts.worktree_tree(local) == new.tree
    assert _user_state(local) == before            # HEAD, index, stash untouched


def test_local_edit_meanwhile_is_a_conflict_and_survives(local, tmp_path):
    remote = str(tmp_path / "remote")
    base = _materialize(local, remote)
    Path(remote, "README.md").write_text("remote\n")
    Path(local, "keep.txt").write_text("USER EDIT\n")
    with pytest.raises(ts.SyncConflictError) as exc:
        _sync_down(local, remote, base)
    assert exc.value.paths == ("keep.txt",)
    assert Path(local, "keep.txt").read_text() == "USER EDIT\n"
    assert Path(local, "README.md").read_text() == "hi\n"             # nothing applied
    assert _git(local, "rev-parse", "--verify", "refs/mo/remote/t/latest")  # remote snap kept


def test_replay_is_a_no_op(local, tmp_path):
    remote = str(tmp_path / "remote")
    base = _materialize(local, remote)
    Path(remote, "README.md").write_text("once\n")
    new, _ = _sync_down(local, remote, base)
    assert ts.apply_delta(local, base, new) is False
    assert Path(local, "README.md").read_text() == "once\n"


def test_interleaved_sync_downs_compose(local, tmp_path):
    """Two nodes share the replica: A's delta lands, then B's lands on top."""
    remote = str(tmp_path / "remote")
    base = _materialize(local, remote)
    Path(remote, "from_a.txt").write_text("a\n")
    snap_a, _ = _sync_down(local, remote, base)
    Path(remote, "from_b.txt").write_text("b\n")
    snap_b, _ = _sync_down(local, remote, snap_a)
    assert Path(local, "from_a.txt").is_file() and Path(local, "from_b.txt").is_file()
    assert ts.worktree_tree(local) == snap_b.tree


def test_agent_commit_is_reported_and_still_arrives_uncommitted(local, tmp_path):
    remote = str(tmp_path / "remote")
    base = _materialize(local, remote)
    _git(remote, "config", "user.email", "agent@x")
    _git(remote, "config", "user.name", "agent")
    Path(remote, "committed.txt").write_text("agent committed this\n")
    _git(remote, "add", "committed.txt")
    _git(remote, "commit", "-q", "-m", "agent commit")
    local_head = _git(local, "rev-parse", "HEAD")
    _new, head_moved = _sync_down(local, remote, base)
    assert head_moved is True
    assert Path(local, "committed.txt").is_file()
    assert _git(local, "rev-parse", "HEAD") == local_head
    assert "?? committed.txt" in _status(local)


# --------------------------------------------------------------------------- ladder


def test_ladder_falls_through_to_squashed(local, tmp_path, monkeypatch):
    attempts: list[tuple[str, ...]] = []
    real = ts._bundle_create

    def sized(repo, *revs, timeout=300):
        attempts.append(revs)
        path = real(repo, *revs, timeout=timeout)
        if "--all" in revs or (len(attempts) == 2):      # full and branch: oversize
            path.write_bytes(path.read_bytes() + b"\0" * 4096)
        return path

    monkeypatch.setattr(ts, "_bundle_create", sized)
    snap = ts.snapshot(local)
    bundle, mode = ts.initial_bundle(local, snap, max_bytes=4000)
    assert mode == "squashed" and attempts[0] == ("--all",) and len(attempts) == 3
    remote = str(tmp_path / "remote")
    tip = ts.bundle_tip(local)
    ts.materialize(remote, bundle, ts.Snap(tip, snap.tree), tip)
    assert ts.worktree_tree(remote) == snap.tree


def test_everything_too_large_raises(local):
    with pytest.raises(ts.SyncTooLargeError, match="MB"):
        ts.initial_bundle(local, ts.snapshot(local), max_bytes=1)
