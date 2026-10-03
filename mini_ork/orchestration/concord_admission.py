"""Conflict-aware epic admission — the ``mini-ork concord admit`` engine (Concord P2b).

Pure, stdlib-only helpers backing the ``admit`` subcommand in ``concord.py``.
The scheduler runs ``MO_SCHED_PRE_DISPATCH_HOOK`` before dispatching an epic and
reads the exit code: 0 = dispatch, 75 = defer back to the queue (no attempt
used), anything else = a failed attempt. ``admit`` exists to answer one
question: *does the epic's declared "Files in scope" overlap an epic already in
progress?* If so it defers (exit 75), preventing two epics from clobbering the
same files.

Design invariants:

- **Fail open everywhere.** An admission bug must never block or fail the
  scheduler. Every internal error (missing env, unreadable kickoff, corrupt or
  unreadable state DB) warns on stderr and admits (exit 0).
- **Read-only on the state DB.** ``admit()`` only SELECTs epics; it never writes.
- **Operator note (spec-vs-code mismatch).** The kickoff documents opting in
  with ``MO_SCHED_PRE_DISPATCH_HOOK="mini-ork concord admit"``, but
  ``mini_ork/scheduler.py::_run_hook`` executes the hook as a *single* argv
  element (``subprocess.Popen([hook], ...)``, no ``shell=True``, no
  ``shlex.split``). As written that string is one executable literally named
  ``mini-ork concord admit`` → ``FileNotFoundError`` → rc 126 → a failed
  attempt, not a defer. This module is correct and self-contained; the operator
  needs a one-line shim (``#!/bin/sh\nexec mini-ork concord admit "$@"``) until
  the scheduler teaches ``_run_hook`` to split arguments. Do NOT edit
  ``scheduler.py`` for this — it is out of scope by the kickoff's own terms.
"""
from __future__ import annotations

import fnmatch
import os
import re
import sqlite3
import subprocess
import sys

__all__ = ["parse_scope", "normalize", "overlaps", "admit"]

#: Default value for ``MINI_ORK_WORKTREES_DIR`` (worktrees are one dir per slug).
_WORKTREES_DIR_DEFAULT = "/Volumes/docker-ssd/ps/mini-ork-worktrees"

#: A markdown ATX heading: up to 3 leading spaces, ``#+``, then the title.
_HEADING_RE = re.compile(r"^\s{0,3}(#+)\s+(.+?)\s*$")
#: An inline-code span. The kickoff lists scope paths as `` `path` `` spans.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
#: A bullet/list marker followed by the first token (the "bullet head").
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+)$")


def _heading(line: str) -> tuple[int, str] | None:
    """Return ``(level, title)`` for an ATX heading line, else ``None``."""
    m = _HEADING_RE.match(line)
    if not m:
        return None
    return len(m.group(1)), m.group(2)


def _strip_commentary(token: str) -> str:
    """Drop trailing ``— commentary`` from a scope token."""
    idx = token.find("—")  # em dash
    if idx != -1:
        token = token[:idx]
    return token.strip()


def _looks_like_path(s: str) -> bool:
    """A scope token is a path when it has a ``/`` or ``.`` and no spaces."""
    if not s or " " in s:
        return False
    return "/" in s or "." in s


def parse_scope(kickoff_text: str) -> list[str]:
    """The paths in the kickoff's scope section.

    The section is the first heading whose text contains "Files in scope"
    (case-insensitive) up to the next heading of the same-or-higher level.
    Within it, take every backtick span or bullet head that looks like a path
    (contains ``/`` or ``.`` and no spaces). Strip trailing ``—`` commentary.
    Globs (``*``, ``**``) are kept verbatim. Returns an ordered, de-duplicated
    list; empty when the kickoff has no such section.
    """
    lines = kickoff_text.splitlines()
    scope_level: int | None = None
    heading_idx = -1
    for i, line in enumerate(lines):
        h = _heading(line)
        if h is not None and "files in scope" in h[1].lower():
            scope_level, heading_idx = h[0], i
            break
    if scope_level is None:
        return []

    paths: list[str] = []
    for line in lines[heading_idx + 1:]:
        h = _heading(line)
        if h is not None and h[0] <= scope_level:
            break  # next heading of same-or-higher level ends the section

        spans = _BACKTICK_RE.findall(line)
        if spans:
            candidates = spans
        else:
            m = _BULLET_RE.match(line)
            if m:
                candidates = [m.group(1).split(None, 1)[0]]
            else:
                candidates = []

        for c in candidates:
            cand = _strip_commentary(c)
            if _looks_like_path(cand) and cand not in paths:
                paths.append(cand)
    return paths


def _is_glob(s: str) -> bool:
    return "*" in s or "?" in s or "[" in s


def overlaps(a: str, b: str) -> bool:
    """Component-aligned prefix overlap, or ``fnmatch`` when either side is a glob.

    ``src`` vs ``src/a.py`` → True; ``src2`` vs ``src/a`` → False. A glob
    (``*``/``?``/``[``) on either side is matched with ``fnmatch.fnmatchcase``.
    """
    a = a.strip().rstrip("/")
    b = b.strip().rstrip("/")
    if not a or not b:
        return False
    if _is_glob(a) or _is_glob(b):
        return fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)
    if a == b:
        return True
    a_slash = a + "/"
    b_slash = b + "/"
    return a_slash.startswith(b_slash) or b_slash.startswith(a_slash)


def normalize(path: str, repo_roots: list[str]) -> str:
    """Make ``path`` repo-relative when it lies under a repo root.

    Absolute paths under any root (the target's git toplevel or a worktree root
    under ``MINI_ORK_WORKTREES_DIR``) become repo-relative, so the same file in
    different worktrees compares equal. Already-relative paths are normalized
    and returned as-is. Globs survive normalization untouched.
    """
    p = path.strip()
    if not p:
        return ""
    if not os.path.isabs(p):
        return os.path.normpath(p).replace(os.sep, "/").lstrip("/")

    norm = os.path.normpath(p)
    for root in sorted((r for r in repo_roots if r), key=len, reverse=True):
        r = os.path.normpath(root)
        if not r:
            continue
        if norm == r or norm.startswith(r + os.sep):
            rel = os.path.relpath(norm, r)
            if rel == ".":
                return ""
            return rel.replace(os.sep, "/")
    return norm.lstrip("/")


def _repo_roots() -> list[str]:
    """The git toplevel of the current directory plus worktree roots under
    ``MINI_ORK_WORKTREES_DIR`` (default ``/Volumes/docker-ssd/ps/mini-ork-worktrees``)."""
    roots: list[str] = []
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            roots.append(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass

    wt_dir = os.environ.get("MINI_ORK_WORKTREES_DIR", _WORKTREES_DIR_DEFAULT)
    try:
        for entry in os.listdir(wt_dir):
            full = os.path.join(wt_dir, entry)
            if os.path.isdir(full):
                roots.append(full)
    except OSError:
        pass
    return roots


def _resolve_kickoff(path: str) -> str:
    """Epics store repo-relative kickoff paths (``kickoffs/auto/x.md``): resolve
    them against ``MINI_ORK_ROOT``, then the git toplevel, then the cwd."""
    if not path or os.path.isabs(path) or os.path.isfile(path):
        return path
    bases = [os.environ.get("MINI_ORK_ROOT", "")]
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            bases.append(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    for base in bases:
        if base and os.path.isfile(os.path.join(base, path)):
            return os.path.join(base, path)
    return path


def _scope_of_file(path: str) -> list[str]:
    with open(_resolve_kickoff(path), encoding="utf-8") as f:
        return parse_scope(f.read())


def admit(db: str, epic_id: str, kickoff_path: str,
          repo_roots: list[str] | None = None) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for admitting ``epic_id``.

    Loads the in-progress epics (``archived_at IS NULL``, ``id != epic_id``)
    that have a readable ``kickoff_path`` and compares their declared scope
    against ``kickoff_path``'s. The first overlap returns
    ``(False, "scope overlap with in-progress <id>: <path_a> ~ <path_b>")``;
    otherwise ``(True, "")``. An epic with an EMPTY scope never blocks and is
    never blocked. Every failure (unreadable kickoff, corrupt DB) warns on
    stderr and fails open to ``(True, "")``.
    """
    try:
        own_scope = _scope_of_file(kickoff_path)
    except OSError as exc:
        print(f"warning: cannot read kickoff {kickoff_path}: {exc}", file=sys.stderr)
        return True, ""
    if not own_scope:
        return True, ""  # empty scope: never blocks, never blocked, prints nothing

    roots = repo_roots if repo_roots is not None else _repo_roots()
    own_norm = [normalize(p, roots) for p in own_scope]

    con: sqlite3.Connection | None = None
    try:
        # Read-only URI: a plain connect() on a missing path would CREATE an
        # empty state DB there — admission must never write anything.
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, kickoff_path FROM epics "
            "WHERE archived_at IS NULL AND status = 'in progress' AND id != ?",
            (epic_id,),
        ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        print(f"warning: cannot read state DB {db}: {exc}", file=sys.stderr)
        return True, ""
    finally:
        if con is not None:
            con.close()

    for row in rows:
        other_id = row["id"]
        other_path = row["kickoff_path"]
        if not other_path:
            continue
        try:
            other_scope = _scope_of_file(other_path)
        except OSError:
            continue  # unreadable in-progress epic is skipped
        if not other_scope:
            continue  # empty scope never blocks
        for a in own_norm:
            for b in (normalize(p, roots) for p in other_scope):
                if overlaps(a, b):
                    return False, f"scope overlap with in-progress {other_id}: {a} ~ {b}"
    return True, ""
