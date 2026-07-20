"""Durable session-transcript store for turn-level resume (durable-dag E4).

Design source: ``internal-docs/architecture/2026-07-15-durable-dag-resume-design.md`` §6.

The claude CLI keeps each conversation as a jsonl transcript under
``~/.claude/projects/<project-slug>/<session_id>.jsonl`` (the slug is the
absolute cwd with path separators turned into dashes). ``claude --resume
<session_id>`` replays that transcript to continue the SAME conversation at the
turn it stopped. But the transcript lives in the *worker's* home — if the
sandbox/worker dies, the transcript dies with it and ``--resume`` fails.

This module makes turn-resume survive that death:

  * ``persist_session(run_dir, session_id)`` copies the live transcript into
    the run dir (``<run_dir>/sessions/<session_id>.jsonl``) at checkpoint and
    on a recoverable failure, returning a portable ``session_ref``.
  * ``restore_session(run_dir, session_ref, session_id)`` copies it back into
    ``~/.claude/projects/<slug>/`` on a fresh sandbox BEFORE the resume, so the
    CLI finds the transcript it expects.

Everything is best-effort and fail-soft: a missing transcript returns ""/False
rather than raising, because turn-resume is a *continuation optimization* — if
it can't happen, the node re-runs from scratch (still correct, just not cheap).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "claude_projects_dir",
    "project_slug",
    "find_session_jsonl",
    "persist_session",
    "restore_session",
]

_SESSIONS_SUBDIR = "sessions"


def _log(msg: str) -> None:
    sys.stderr.write(f"session_store: {msg}\n")


def claude_projects_dir() -> Path:
    """``~/.claude/projects`` (honors ``CLAUDE_CONFIG_DIR`` when the CLI uses a
    non-default config home). The base for every per-project transcript dir."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(cfg) if cfg else Path(os.path.expanduser("~")) / ".claude"
    return base / "projects"


def project_slug(cwd: Optional[str] = None) -> str:
    """Claude's per-project dir name for an absolute cwd.

    Observed transform (e.g. ``/Volumes/docker-ssd/ps/mini-ork`` →
    ``-Volumes-docker-ssd-ps-mini-ork``): the absolute path with every path
    separator replaced by ``-``. We mirror that; ``find_session_jsonl`` also
    globs all project dirs as a fallback so an imperfect slug never blocks a
    lookup — only ``restore_session`` needs the exact slug (to pick a target),
    and there the current cwd is authoritative."""
    p = os.path.abspath(cwd or os.getcwd())
    return p.replace(os.sep, "-")


def find_session_jsonl(
    session_id: str, *, cwd: Optional[str] = None, projects_dir: Optional[Path] = None
) -> Optional[Path]:
    """Locate the live transcript for ``session_id``.

    Checks the cwd-derived project dir first (the common case), then globs
    every project dir (covers a resume whose cwd differs from where the
    session was born). Returns None if no transcript file exists."""
    if not session_id:
        return None
    base = projects_dir or claude_projects_dir()
    if not base.is_dir():
        return None
    # 1. the cwd-derived project dir
    direct = base / project_slug(cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    # 2. glob every project dir (cwd may differ from session origin)
    try:
        for hit in base.glob(f"*/{session_id}.jsonl"):
            if hit.is_file():
                return hit
    except OSError as e:
        _log(f"glob failed under {base}: {e}")
    return None


def persist_session(
    run_dir: str, session_id: str, *, cwd: Optional[str] = None,
    projects_dir: Optional[Path] = None,
) -> str:
    """Copy the live transcript into the run dir. Returns a portable
    ``session_ref`` (``sessions/<session_id>.jsonl``, relative to run_dir) on
    success, or "" if there is nothing to persist. Never raises."""
    if not run_dir or not session_id:
        return ""
    src = find_session_jsonl(session_id, cwd=cwd, projects_dir=projects_dir)
    if src is None:
        return ""
    rel = os.path.join(_SESSIONS_SUBDIR, f"{session_id}.jsonl")
    dst = Path(run_dir) / rel
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as e:
        _log(f"persist_session: copy {src} -> {dst} failed: {e}")
        return ""
    return rel


def restore_session(
    run_dir: str, session_ref: str, session_id: str, *, cwd: Optional[str] = None,
    projects_dir: Optional[Path] = None,
) -> bool:
    """Copy a persisted transcript back into ``~/.claude/projects/<slug>/`` so
    ``claude --resume <session_id>`` finds it on a fresh sandbox. The target
    project dir is derived from the CURRENT cwd (where the resume runs).
    Returns True on success. Never raises.

    Idempotent: if a live transcript already exists for the session, this is a
    no-op success (the sandbox never died)."""
    if not run_dir or not session_ref or not session_id:
        return False
    # already live → nothing to restore
    if find_session_jsonl(session_id, cwd=cwd, projects_dir=projects_dir) is not None:
        return True
    src = Path(run_dir) / session_ref
    if not src.is_file():
        _log(f"restore_session: persisted transcript missing: {src}")
        return False
    base = projects_dir or claude_projects_dir()
    dst_dir = base / project_slug(cwd)
    dst = dst_dir / f"{session_id}.jsonl"
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as e:
        _log(f"restore_session: copy {src} -> {dst} failed: {e}")
        return False
    return True
