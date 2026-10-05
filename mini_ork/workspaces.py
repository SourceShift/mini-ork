"""Task-isolation workspaces: each run gets its own git worktree + branch.

This module is the IPC for the Zed S5 Merge / Discard surface; S4 wires it
into the launch path so each run started from a Zed thread edits and commits
on its own ``mini-ork/<run_id>`` branch instead of trampling the user's
checkout. Two distinct concepts share the ``Workspace`` name:

* ``mini_ork.runtime.sandbox.Workspace`` — Protocol, sandbox backends.
* ``mini_ork.workspaces.Workspace`` — frozen dataclass, task worktrees.

Module-scoped names keep the collision at bay; consumers import explicitly
(``from mini_ork.workspaces import Workspace, create, status, ...``).
``__all__`` deliberately omits ``Workspace`` so a wildcard import cannot
resurface the sandbox-backend class under a confusing alias.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Workspace:
    """One task-isolation workspace — one run, one worktree, one branch."""

    run_id: str
    path: Path
    branch: str
    base_branch: str
    base_sha: str
    project: Path


def _git(args: list[str], cwd: Path | str, timeout: int = 120) -> tuple[int, str, str]:
    """Run one git subcommand, return ``(rc, stdout, stderr)``.

    A single seam so tests can exercise the real subprocess path without
    mocking. ``cwd`` is the working tree the command operates on.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return (124, "", f"git timeout: {exc}")
    return (proc.returncode, proc.stdout, proc.stderr)


def _is_git_repo(project: Path) -> bool:
    rc, _, _ = _git(["rev-parse", "--show-toplevel"], project)
    return rc == 0


def _record_path(home: Path, run_id: str) -> Path:
    return home / "worktrees" / f"{run_id}.json"


def _workspace_root(home: Path, run_id: str) -> Path:
    return home / "worktrees" / run_id


def _setup_hook(home: Path, worktree: Path, *, timeout: int = 600) -> dict[str, Any] | None:
    """Run ``<home>/worktree-setup.sh`` inside the new worktree if present.

    Operator-owned (deps install, .env copy, DB migrations). Failure is
    reported but does NOT delete the worktree — the user can fix and retry.
    """
    hook = home / "worktree-setup.sh"
    if not hook.is_file():
        return None
    try:
        proc = subprocess.run(
            [str(hook)],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ran": True,
            "rc": proc.returncode,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"ran": True, "rc": 124, "error": "timeout"}


def _write_record(home: Path, ws: Workspace) -> Path:
    record = _record_path(home, ws.run_id)
    record.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": ws.run_id,
        "path": str(ws.path),
        "branch": ws.branch,
        "base_branch": ws.base_branch,
        "base_sha": ws.base_sha,
        "project": str(ws.project),
    }
    record.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return record


def _read_record(home: Path, run_id: str) -> dict[str, Any] | None:
    record = _record_path(home, run_id)
    if not record.is_file():
        return None
    try:
        return json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def create(project: Path, home: Path, run_id: str) -> Workspace:
    """Mint a fresh task worktree on its own ``mini-ork/<run_id>`` branch.

    The worktree lives at ``<home>/worktrees/<run_id>``; the base is the
    project's current branch (detached → the sha). If the project is not
    a git repo (or the path does not exist on disk), ``RuntimeError`` with
    git's stderr — the caller decides how to recover (the MCP ``start_run``
    handler falls back to in-place with a ``note``).
    """
    if not project.exists():
        raise RuntimeError(f"not a git repo: {project} (path does not exist)")
    if not _is_git_repo(project):
        rc, _, err = _git(["rev-parse", "--show-toplevel"], project)
        raise RuntimeError(f"not a git repo: {project} (rc={rc}, stderr={err.strip()!r})")
    rc, head_sha, err = _git(["rev-parse", "HEAD"], project)
    if rc != 0:
        raise RuntimeError(f"could not read HEAD: {err.strip()}")
    rc, branch_name, err = _git(["symbolic-ref", "--short", "HEAD"], project)
    base_branch = branch_name.strip() if rc == 0 else head_sha.strip()
    base_sha = head_sha.strip()
    worktree = _workspace_root(home, run_id)
    branch = f"mini-ork/{run_id}"
    rc, _, err = _git(
        ["worktree", "add", "-b", branch, str(worktree), base_sha], project, timeout=300
    )
    if rc != 0:
        raise RuntimeError(f"worktree add failed: {err.strip()}")
    ws = Workspace(
        run_id=run_id,
        path=worktree,
        branch=branch,
        base_branch=base_branch,
        base_sha=base_sha,
        project=project,
    )
    _write_record(home, ws)
    setup = _setup_hook(home, worktree)
    if setup is not None:
        # Augment the record with the last setup result for status() to surface.
        record = _record_path(home, run_id)
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        payload["setup"] = setup
        record.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return ws


def load(home: Path, run_id: str) -> Workspace | None:
    """Return the workspace for ``run_id`` if its worktree directory still exists."""
    raw = _read_record(home, run_id)
    if raw is None:
        return None
    path = Path(raw["path"])
    if not path.is_dir():
        return None
    return Workspace(
        run_id=raw["run_id"],
        path=path,
        branch=raw["branch"],
        base_branch=raw["base_branch"],
        base_sha=raw["base_sha"],
        project=Path(raw["project"]),
    )


def list_open(home: Path) -> list[Workspace]:
    """All workspaces whose worktree directory still exists on disk."""
    out: list[Workspace] = []
    records_dir = home / "worktrees"
    if not records_dir.is_dir():
        return out
    for record in records_dir.glob("*.json"):
        ws = load(home, record.stem)
        if ws is not None:
            out.append(ws)
    return out


def status(ws: Workspace) -> dict[str, Any]:
    """Snapshot of one workspace's branch vs base."""
    if not ws.path.is_dir():
        return {"exists": False}
    rc, ahead, _ = _git(["rev-list", "--count", f"{ws.base_sha}..{ws.branch}"], ws.path)
    commits_ahead = int(ahead.strip()) if rc == 0 else 0
    rc, dirty, _ = _git(["status", "--porcelain"], ws.path)
    uncommitted = [line for line in dirty.splitlines() if line.strip()]
    rc, numstat, _ = _git(["diff", "--numstat", ws.base_sha], ws.path)
    files: list[dict[str, Any]] = []
    added = 0
    removed = 0
    if rc == 0:
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                a = int(parts[0]) if parts[0] != "-" else 0
                r = int(parts[1]) if parts[1] != "-" else 0
            except ValueError:
                continue
            added += a
            removed += r
            files.append({"path": parts[2], "added": a, "removed": r})
    return {
        "exists": True,
        "commits_ahead": commits_ahead,
        "uncommitted": uncommitted,
        "added": added,
        "removed": removed,
        "files": files,
    }


def merge(ws: Workspace, message: str) -> dict[str, Any]:
    """Fast-forward (or merge-commit) ``ws.branch`` into ``ws.project``."""
    if not ws.path.is_dir():
        return {"ok": False, "error": f"worktree gone: {ws.path}"}
    project = ws.project
    # Step 1: commit any uncommitted changes in the worktree.
    rc, dirty, _ = _git(["status", "--porcelain"], ws.path)
    if rc == 0 and dirty.strip():
        rc, _, err = _git(["add", "-A"], ws.path)
        if rc != 0:
            return {"ok": False, "error": f"git add failed: {err.strip()}"}
        # The user's identity when the repo has one; mini-ork's only as a fallback.
        rc_id, email, _ = _git(["config", "user.email"], ws.path)
        identity = [] if rc_id == 0 and email.strip() else [
            "-c", "user.name=mini-ork", "-c", "user.email=mini-ork@localhost"]
        rc, _, err = _git([*identity, "commit", "-m", message], ws.path)
        if rc != 0:
            return {"ok": False, "error": f"commit failed: {err.strip()}"}
    # Step 2: nothing-to-merge → discard.
    rc, ahead, _ = _git(["rev-list", "--count", f"{ws.base_sha}..{ws.branch}"], ws.path)
    if rc != 0:
        return {"ok": False, "error": "could not count commits ahead"}
    if int(ahead.strip() or 0) == 0:
        discard(ws)
        return {"ok": True, "merged": None}
    # Step 3: refuse when the project's current branch is not the base branch.
    rc, current_branch, _ = _git(["symbolic-ref", "--short", "HEAD"], project)
    if rc != 0 or current_branch.strip() != ws.base_branch:
        return {
            "ok": False,
            "error": f"project is on {current_branch.strip()!r}, expected {ws.base_branch!r}",
        }
    # Step 4: refuse when the project has dirty overlapping files.
    rc, changed_paths, _ = _git(["diff", "--name-only", ws.base_sha, ws.branch], project)
    changed_paths = set((changed_paths or "").splitlines())
    rc, dirty_paths_raw, _ = _git(["status", "--porcelain"], project)
    dirty_paths = {line[3:].split(" -> ", 1)[-1] for line in dirty_paths_raw.splitlines() if line.strip()}
    overlap = changed_paths & dirty_paths
    if overlap:
        return {"ok": False, "error": f"project has dirty overlapping files: {sorted(overlap)}"}
    # Uncommitted edits to tracked files (untracked files don't count): only a
    # clean fast-forward is safe — a conflicted merge started over local edits
    # may not restore them on abort.
    tracked_dirty = any(line[:2] != "??" for line in dirty_paths_raw.splitlines() if line.strip())
    # Step 5: try ff-only first; fall back to no-ff.
    rc, _, err = _git(["merge", "--ff-only", ws.branch], project)
    mode = "fast-forward"
    if rc != 0 and tracked_dirty:
        return {"ok": False, "error": "the project has uncommitted changes and this merge "
                "is not a fast-forward — commit or stash them, then merge again"}
    if rc != 0:
        rc, _, err = _git(["merge", "--no-ff", "-m", message, ws.branch], project)
        mode = "merge"
        if rc != 0:
            _git(["merge", "--abort"], project)
            return {"ok": False, "error": f"conflicts in merge: {err.strip()}"}
    rc, merged_sha, _ = _git(["rev-parse", "HEAD"], project)
    merged = merged_sha.strip() if rc == 0 else None
    _git(["worktree", "remove", "--force", str(ws.path)], project)
    _git(["branch", "-D", ws.branch], project)
    record = _record_path(ws.path.parent.parent, ws.run_id)
    if record.is_file():
        record.unlink()
    return {"ok": True, "merged": merged, "mode": mode}


def discard(ws: Workspace) -> dict[str, Any]:
    """Remove the worktree, delete the branch, delete the record — idempotent."""
    project = ws.project
    if ws.path.is_dir():
        _git(["worktree", "remove", "--force", str(ws.path)], project)
    rc, _, _ = _git(["branch", "-D", ws.branch], project)
    if rc != 0 and not ws.path.is_dir():
        # The branch may already be gone; only swallow if there is no worktree
        # to clean up. A worktree + missing branch is a real error worth raising.
        # (Real-world: ``git branch -D`` rc=1 with "not found." is benign.)
        pass
    record = _record_path(ws.path.parent.parent, ws.run_id)
    if record.is_file():
        record.unlink()
    return {"ok": True}