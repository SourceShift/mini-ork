"""Git diff helpers for the review runtime (subprocess seam).

Extracted from ``mini_ork/pre_push_review.py`` (parity port of
``lib/pre_push_review.sh`` lines 366-397).
"""
from __future__ import annotations

import os
import re
import subprocess

# ─────────────────────────────────────────────────────────────────────────
# Git diff helpers
# ─────────────────────────────────────────────────────────────────────────

def _compute_base(source_sha: str, target_branch: str, cwd: str | os.PathLike[str]) -> str:
    """Mirror the bash merge-base fallback chain at pre_push_review.sh:370-378.

    Order:
      1. ``origin/<target_branch>``
      2. ``main``
      3. ``<source_sha>^`` (parent) — only if the prior attempts returned
         empty (no common ancestor at all).
    """
    try:
        r = subprocess.run(
            ["git", "merge-base", source_sha, f"origin/{target_branch}"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        r = subprocess.run(
            ["git", "merge-base", source_sha, "main"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", f"{source_sha}^"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except OSError:
        pass
    return ""


def _git_diff(base: str, source_sha: str, cwd: str | os.PathLike[str]) -> str:
    """Mirror ``git diff $base..$source_sha``. Falls back to ``git show``."""
    try:
        if base:
            r = subprocess.run(
                ["git", "diff", f"{base}..{source_sha}"],
                cwd=str(cwd), capture_output=True, text=True,
            )
        else:
            r = subprocess.run(
                ["git", "show", source_sha],
                cwd=str(cwd), capture_output=True, text=True,
            )
        return r.stdout if r.returncode == 0 else ""
    except OSError:
        return ""


def _git_shortstat(base: str, source_sha: str, cwd: str | os.PathLike[str]) -> str:
    """Mirror ``git diff --shortstat``."""
    try:
        r = subprocess.run(
            ["git", "diff", "--shortstat", f"{base}..{source_sha}"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        return r.stdout if r.returncode == 0 else ""
    except OSError:
        return ""


def _count_diff_lines(diff_text: str, *, base: str, source_sha: str, cwd: str | os.PathLike[str]) -> tuple[int, int, int]:
    """Return (files_changed, lines_added, lines_removed).

    Mirrors the bash sequence at pre_push_review.sh:389-397:
      * files_changed from ``grep -oE '[0-9]+ file'`` on ``--shortstat``
        (first match; falls back to 0 if absent).
      * lines_added from ``grep -cE "^\\+[^+]"`` (a plain integer, no
        ``|| echo 0`` chaining).
      * lines_removed from ``grep -cE "^-[^-]"``.
    """
    shortstat = _git_shortstat(base, source_sha, cwd)
    m = re.search(r"(\d+) file", shortstat or "")
    files_changed = int(m.group(1)) if m else 0
    # Count directly from the diff text to mirror the grep -cE semantics.
    # bash uses ``grep -cE "^\+[^+]"`` (and the ``-`` analog): the regex
    # requires a non-``+`` character immediately after the leading ``+``,
    # so a bare ``+`` line (often a blank-context marker) is NOT counted.
    # Mirror that exactly with ``re.match`` instead of ``startswith``.
    lines_added = sum(
        1 for line in diff_text.split("\n")
        if re.match(r"^\+[^+]", line)
    )
    lines_removed = sum(
        1 for line in diff_text.split("\n")
        if re.match(r"^-[^-]", line)
    )
    return files_changed, lines_added, lines_removed
