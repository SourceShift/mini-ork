"""Prune stale pytest basetemp directories (``$TMPDIR/pytest-of-<user>/pytest-N``).

pytest keeps the last 3 basetemps, but it never deletes one whose lock file is
still present, and a lock only counts as stale after 3 days. Sessions killed
mid-run (loop timeouts, verifier aborts) leave their lock behind, so under many
concurrent sessions the directory grows without bound: 37 GB in 3 days on one
laptop, filling the system disk to 100%.

A live session touches its basetemp every time a test creates a tmp_path, so an
mtime older than ``max_age_s`` means no session is writing there.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path


def prune_stale_basetemps(
    root: Path,
    *,
    max_age_s: float,
    keep: Path | None = None,
    now: float | None = None,
) -> list[Path]:
    """Delete ``root/pytest-*`` dirs untouched for ``max_age_s``; never ``keep``."""
    if not root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - max_age_s
    keep_resolved = keep.resolve() if keep is not None else None
    removed: list[Path] = []
    for entry in root.glob("pytest-*"):
        if entry.is_symlink() or not entry.is_dir():
            continue  # `pytest-current` is a symlink to the live session
        if keep_resolved is not None and entry.resolve() == keep_resolved:
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry)
        except OSError:
            continue  # raced with another session's cleanup, or still in use
        removed.append(entry)
    return removed
