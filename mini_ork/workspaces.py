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

Slice Z-W1 (2026-10-06): the worktree lives under Zed's
``git.worktree_directory`` (``../worktrees/<project-name>/<name>`` by default)
rather than ``<home>/worktrees/<run_id>``, and a thread started in a Zed
linked worktree **adopts** the linked worktree (no new one is minted). The
record stays at ``<home>/worktrees/<run_id>.json`` — that index dir never
moved — so old records with old paths keep working: ``Workspace.path``
stores whatever path the record held.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
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
    #: ``True`` when the workspace adopted an existing linked worktree
    #: (``adopt``) instead of minting a new one (``create``). ``merge`` and
    #: ``discard`` branch on this — Zed owns the worktree and the branch.
    adopted: bool = False
    #: ``True`` when the linked worktree had a clean ``git status --porcelain``
    #: at adopt time. ``discard`` on an adopted workspace refuses when the
    #: user had dirty edits before the run started.
    clean_at_start: bool = False
    #: ``.mini-ork`` home the record is stored under. ``merge``/``discard``
    #: resolve the record directly via ``_record_path(home, run_id)`` — the
    #: pre-W1 trick of ``ws.path.parent.parent`` does not work for worktrees
    #: living under ``worktree_dir(project)`` (Z-W1). Defaults to an empty
    #: path so existing callers keep compiling; ``create``/``adopt`` always
    #: set it.
    home: Path = field(default_factory=Path)


def _git(args: list[str], cwd: Path | str, timeout: int = 120) -> tuple[int, str, str]:
    """Run one git subcommand, return ``(rc, stdout, stderr)``.

    A single seam so tests can exercise the real subprocess path without
    mocking. ``cwd`` is the working tree the command operates on. A
    non-existent ``cwd`` (Z-W1: callers probe ``main_checkout`` /
    ``is_linked_worktree`` against unknown paths) surfaces as ``rc=128``
    instead of a ``FileNotFoundError`` so callers can fall through.
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
    except FileNotFoundError as exc:
        return (128, "", f"git cwd not found: {exc}")
    return (proc.returncode, proc.stdout, proc.stderr)


def _is_git_repo(project: Path) -> bool:
    rc, _, _ = _git(["rev-parse", "--show-toplevel"], project)
    return rc == 0


# ── linked-worktree detection (Z-W1) ─────────────────────────────────────────


def main_checkout(path: Path) -> Path | None:
    """The repo's main worktree root, or ``None`` outside a git checkout.

    Resolves ``git rev-parse --path-format=absolute --git-common-dir`` (the
    shared ``.git`` dir for the repo — all worktrees' ``$GIT_COMMON_DIR``
    points here) and returns its parent when it ends in ``.git``. ``None``
    when the path is not inside a git repo.
    """
    rc, out, _ = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    if rc != 0:
        return None
    common = Path(out.strip())
    if common.name != ".git":
        return None
    return common.parent


def is_linked_worktree(path: Path) -> bool:
    """``True`` when ``path`` is a git linked worktree (not the main checkout).

    ``git rev-parse --git-dir`` returns the worktree-local ``.git`` dir
    (the file containing ``gitdir: …``) for linked worktrees and the shared
    ``.git/`` dir for the main checkout; ``--git-common-dir`` always returns
    the shared one. They differ iff ``path`` is a linked worktree.
    """
    rc_dir, git_dir, _ = _git(["rev-parse", "--path-format=absolute", "--git-dir"], path)
    if rc_dir != 0:
        return False
    rc_common, git_common, _ = _git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], path
    )
    if rc_common != 0:
        return False
    return Path(git_dir.strip()) != Path(git_common.strip())


# ── worktree location (Z-W1) ────────────────────────────────────────────────


_DEFAULT_WORKTREE_REL = "../worktrees"


def _worktree_dir_from_settings(
    main_checkout_path: Path, *, project: bool = True
) -> Path | None:
    """Read Zed's ``git.worktree_directory`` from settings, lenient.

    Precedence per spec: project ``.zed/settings.json`` → ``ZED_SETTINGS``
    env (already substituted by Zed into a file path) → user
    ``~/.config/zed/settings.json``. Parses leniently because Zed's default
    settings template ships with ``//`` comments and trailing commas.

    Returns the directory as written (``"../worktrees"``, ``"~/code/wts"``,
    absolute paths are not rejected). ``None`` when the key is absent or
    unparseable; the caller falls back to :data:`_DEFAULT_WORKTREE_REL`.
    """
    candidates: list[Path] = []
    if project:
        candidates.append(main_checkout_path / ".zed" / "settings.json")
    env_path = os.environ.get("ZED_SETTINGS")
    if env_path:
        candidates.append(Path(env_path))
    else:
        candidates.append(Path.home() / ".config" / "zed" / "settings.json")

    # Lazy import: ``mini_ork.cli.zed_cmd`` is heavy (it imports the whole
    # ``cli`` package). Keep workspaces importable without it.
    try:
        from mini_ork.cli.zed_cmd import _load_settings
    except Exception:  # noqa: BLE001 — fallback handles missing helper
        return None

    for candidate in candidates:
        try:
            data = _load_settings(str(candidate), lenient=True)[0]
        except Exception:  # noqa: BLE001 — defensive against helper crashes
            continue
        if not isinstance(data, dict):
            continue
        git = data.get("git")
        if not isinstance(git, dict):
            continue
        val = git.get("worktree_directory")
        if isinstance(val, str) and val.strip():
            return Path(val.strip()).expanduser()
    return None


def worktree_dir(project: Path) -> Path:
    """Where ``create`` puts a NEW task worktree for the given project.

    Precedence (kickoff §worktree_dir):
      1. ``MO_WORKTREE_DIR`` env var (absolute → as-is; relative → main checkout).
      2. Zed's ``git.worktree_directory`` from project → user settings.
      3. ``../worktrees`` (relative to the project's main checkout).

    When the resolved directory is OUTSIDE the main checkout, append the
    main checkout's directory name — Zed's rule (one worktree dir can be
    shared by several repos; the suffix keeps them from colliding).
    """
    main = main_checkout(project)
    if main is None:
        # Fall back to the project's own parent. ``create`` already gates on
        # ``_is_git_repo``, so this branch is rare.
        main = project.resolve().parent

    env = os.environ.get("MO_WORKTREE_DIR")
    if env:
        raw = Path(env).expanduser()
        if not raw.is_absolute():
            raw = (main / env).resolve()
        else:
            raw = raw.resolve()
    else:
        raw_setting = _worktree_dir_from_settings(main)
        raw_setting_str = str(raw_setting) if raw_setting is not None else _DEFAULT_WORKTREE_REL
        raw = Path(os.path.expanduser(raw_setting_str))
        if not raw.is_absolute():
            raw = (main / raw_setting_str).resolve()
        else:
            raw = raw.resolve()

    try:
        inside = raw.is_relative_to(main)
    except AttributeError:  # py<3.9 fallback
        inside = str(raw).startswith(str(main))
    if not inside:
        raw = raw / main.name
    return raw


def task_name(title: str, run_id: str) -> str:
    """``"<slug of title, ≤40>-<last 6 of run_id>"`` — the worktree directory
    name used by :func:`create` for new task worktrees.

    Slug rule: lowercase, ``[^a-z0-9]+`` → ``-``, collapse, trim trailing
    ``-``, cap at 40 chars. Empty title → just ``"<last 6 of run_id>"``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:40].rstrip("-")
    suffix = run_id[-6:] if len(run_id) >= 6 else run_id
    return f"{slug}-{suffix}" if slug else suffix


def _record_path(home: Path, run_id: str) -> Path:
    """Record index path — always ``<home>/worktrees/<run_id>.json``.

    Unchanged by Z-W1: the index dir does not move with the worktree. Old
    records with ``path: <home>/worktrees/<run_id>`` keep working because
    :attr:`Workspace.path` stores whatever path the record held.
    """
    return home / "worktrees" / f"{run_id}.json"


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
        "adopted": ws.adopted,
        "clean_at_start": ws.clean_at_start,
        "home": str(ws.home) if str(ws.home) else str(home),
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


def create(
    project: Path, home: Path, run_id: str, *, name: str | None = None
) -> Workspace:
    """Mint a fresh task worktree on its own ``mini-ork/<run_id>`` branch.

    The worktree lives at :func:`worktree_dir` ``/ (name or run_id)`` —
    a name override (``task_name(title, run_id)``) keeps the directory
    readable in Zed's worktree picker (``title-123abc`` instead of
    ``run-1700-123abc``). The record stays at
    ``<home>/worktrees/<run_id>.json``; ``load`` reads the path back from
    the record so old records with the legacy
    ``<home>/worktrees/<run_id>`` path keep working.

    The base is the project's current branch (detached → the sha). If the
    project is not a git repo (or the path does not exist on disk),
    ``RuntimeError`` with git's stderr — the caller decides how to recover
    (the MCP ``start_run`` handler falls back to in-place with a ``note``).
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
    # Resolve the directory of the project's main checkout (NOT the project
    # path itself — a linked worktree passed as ``project`` should still
    # mint the new worktree under the main checkout's worktree_dir).
    main = main_checkout(project) or project.resolve()
    wt_dir = worktree_dir(main)
    worktree = wt_dir / (name or run_id)
    worktree.parent.mkdir(parents=True, exist_ok=True)
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
        project=main,
        home=home,
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


def adopt(cwd: Path, home: Path, run_id: str) -> Workspace:
    """Adopt an already-checked-out linked worktree as the run's workspace.

    The branch becomes the worktree's current branch (or ``mini-ork/<run_id>``
    on detached HEAD); ``base_branch`` is the **main** checkout's current
    branch; ``base_sha`` is HEAD now; ``project`` is the main checkout;
    ``adopted=True``; ``clean_at_start`` = ``git status --porcelain`` was
    empty at adopt time. ``RuntimeError`` when ``cwd`` is not a linked
    worktree — the caller must fall through to :func:`create` in that case.
    """
    if not is_linked_worktree(cwd):
        raise RuntimeError(f"not a linked worktree: {cwd}")
    main = main_checkout(cwd)
    if main is None:
        raise RuntimeError(f"no main checkout for: {cwd}")

    # Branch = cwd's current branch, or mint a new one when HEAD is detached.
    rc, branch_name, _ = _git(["symbolic-ref", "--short", "HEAD"], cwd)
    if rc == 0 and branch_name.strip():
        branch = branch_name.strip()
    else:
        branch = f"mini-ork/{run_id}"
        rc_sw, _, err_sw = _git(["switch", "-c", branch], cwd)
        if rc_sw != 0:
            raise RuntimeError(f"git switch -c failed: {err_sw.strip()}")

    rc, head_sha, _ = _git(["rev-parse", "HEAD"], cwd)
    if rc != 0:
        raise RuntimeError("could not read HEAD of linked worktree")
    base_sha = head_sha.strip()

    rc, base_branch_raw, _ = _git(["symbolic-ref", "--short", "HEAD"], main)
    base_branch = base_branch_raw.strip() if rc == 0 else base_sha

    rc, dirty, _ = _git(["status", "--porcelain"], cwd)
    clean_at_start = rc == 0 and not dirty.strip()

    ws = Workspace(
        run_id=run_id,
        path=cwd.resolve(),
        branch=branch,
        base_branch=base_branch,
        base_sha=base_sha,
        project=main,
        adopted=True,
        clean_at_start=clean_at_start,
        home=home,
    )
    _write_record(home, ws)
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
        adopted=bool(raw.get("adopted", False)),
        clean_at_start=bool(raw.get("clean_at_start", False)),
        home=Path(raw["home"]) if raw.get("home") else home,
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
    # ``git diff`` never sees untracked files, so a run that only creates
    # files would read +0 −0; count their lines too (merge commits them).
    rc, untracked, _ = _git(["ls-files", "--others", "--exclude-standard"], ws.path)
    if rc == 0:
        for rel in untracked.splitlines():
            if not rel.strip():
                continue
            try:
                data = (ws.path / rel).read_bytes()
            except OSError:
                continue
            a = 0 if b"\0" in data[:8192] else data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
            added += a
            files.append({"path": rel, "added": a, "removed": 0})
    return {
        "exists": True,
        "commits_ahead": commits_ahead,
        "uncommitted": uncommitted,
        "added": added,
        "removed": removed,
        "files": files,
    }


def merge(ws: Workspace, message: str) -> dict[str, Any]:
    """Fast-forward (or merge-commit) ``ws.branch`` into ``ws.project``.

    For an adopted workspace (``ws.adopted``): the merge still happens
    against the project's current branch (the main checkout), but the
    worktree + branch stay — only the record is deleted.
    """
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
    if not ws.adopted:
        _git(["worktree", "remove", "--force", str(ws.path)], project)
        _git(["branch", "-D", ws.branch], project)
    record = _record_path(ws.home, ws.run_id)
    if record.is_file():
        record.unlink()
    return {"ok": True, "merged": merged, "mode": mode}


def discard(ws: Workspace) -> dict[str, Any]:
    """Remove the worktree, delete the branch, delete the record — idempotent.

    For an adopted workspace: reset the worktree to ``ws.base_sha`` and
    clean untracked files when the worktree was clean at start, otherwise
    refuse (the user had dirty edits before the run started and we will
    not throw their work away). In either case the worktree directory and
    its branch are not touched — Zed owns them.
    """
    project = ws.project
    if ws.adopted:
        if not ws.clean_at_start:
            return {
                "ok": False,
                "error": "this worktree had changes before the run — discard them in Zed's "
                         "Git panel",
            }
        if ws.path.is_dir():
            _git(["reset", "--hard", ws.base_sha], ws.path)
            _git(["clean", "-fd"], ws.path)
        record = _record_path(ws.home, ws.run_id)
        if record.is_file():
            record.unlink()
        return {"ok": True}
    if ws.path.is_dir():
        _git(["worktree", "remove", "--force", str(ws.path)], project)
    rc, _, _ = _git(["branch", "-D", ws.branch], project)
    if rc != 0 and not ws.path.is_dir():
        # The branch may already be gone; only swallow if there is no worktree
        # to clean up. A worktree + missing branch is a real error worth raising.
        # (Real-world: ``git branch -D`` rc=1 with "not found." is benign.)
        pass
    record = _record_path(ws.home, ws.run_id)
    if record.is_file():
        record.unlink()
    return {"ok": True}