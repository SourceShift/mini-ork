"""Shared env resolution + issue row shape helpers for the review runtime.

Extracted from ``mini_ork/pre_push_review.py`` (parity port of
``lib/pre_push_review.sh``). All functions keep their original bash-parity
semantics byte-identical; only the module location changed.
"""
from __future__ import annotations

import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────
# Env resolution (mirrors bash lines 26-27)
# ─────────────────────────────────────────────────────────────────────────

def _resolve_db() -> str:
    """Return the state.db path the bash script would pick.

    Resolution order (mirrors ``lib/pre_push_review.sh:27``):
      $MINI_ORK_DB → ${MINI_ORK_HOME:-.mini-ork}/state.db
    """
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return os.path.join(home, "state.db")


def _resolve_home() -> str:
    """Return the .mini-ork home, mirrors ``MINI_ORK_HOME:-.mini-ork``."""
    return os.environ.get("MINI_ORK_HOME") or ".mini-ork"


def _resolve_root() -> str:
    """Return MINI_ORK_ROOT: env var or parent of mini_ork/ package.

    Mirrors bash ``cd "$(dirname "${BASH_SOURCE[0]}")/.."`` semantics
    relative to lib/. The Python package parent (``mini_ork/``) maps to
    the repo root.
    """
    env_root = os.environ.get("MINI_ORK_ROOT")
    if env_root:
        return env_root
    # This module lives at mini_ork/review/common.py; three parents up is
    # the repo root (same value the original mini_ork/pre_push_review.py
    # computed with two parents).
    return str(Path(__file__).resolve().parent.parent.parent)


# ─────────────────────────────────────────────────────────────────────────
# Issue row shape helpers (mirrors bash JSONL contract)
# ─────────────────────────────────────────────────────────────────────────

# Field truncations that bash applies at INSERT time (lines 451-454).
_TITLE_MAX = 300
_DESCRIPTION_MAX = 2000
_SUGGESTED_FIX_MAX = 1000


def _truncate_issue(issue: dict) -> dict:
    """Truncate title/description/suggested_fix at the bash contract lengths.

    Mirrors the bash heredoc at ``pre_push_review.sh:451-454``.
    """
    return {
        "lens": issue.get("lens", "?"),
        "severity": issue.get("severity", "medium"),
        "file": issue.get("file") if issue.get("file") is not None else "?",
        "line": issue.get("line"),
        "title": (issue.get("title") or "")[:_TITLE_MAX],
        "description": (issue.get("description") or "")[:_DESCRIPTION_MAX],
        "suggested_fix": (issue.get("suggested_fix") or "")[:_SUGGESTED_FIX_MAX],
    }


def _read_diff(diff_or_path: str | os.PathLike[str]) -> str:
    """Accept either a diff string or a path to a diff file.

    Mirrors the bash convention where each ``_check_*`` reads the diff
    path. Python accepts both so callers (tests, callers) can pass either.
    """
    if isinstance(diff_or_path, (str, bytes, bytearray)):
        text = diff_or_path if isinstance(diff_or_path, str) else diff_or_path.decode("utf-8", "replace")
        if "\n" in text or text.startswith("diff --git "):
            return text
    else:
        text = str(diff_or_path)
    try:
        path = Path(text)
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return text
    if isinstance(diff_or_path, (str, bytes, bytearray)):
        return text
    return str(diff_or_path)


def _resolve_check_path(path: str, cwd: str | os.PathLike[str] | None) -> str:
    """Resolve a file path from the diff against the review cwd.

    The bash subprocess reads ``bash -n <file>`` from its cwd; the Python
    port must do the same so file paths in the diff resolve to the same
    on-disk files in both ports.
    """
    if cwd is None or os.path.isabs(path):
        return path
    return os.path.join(str(cwd), path)
