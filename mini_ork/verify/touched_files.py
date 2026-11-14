"""The run's change surface — one definition, shared by scoped verifiers.

``verifiers/typecheck.py`` established the contract: a verifier must check the
files *this run* changed, never the whole tree, because a red baseline elsewhere
reddens every lane for a defect the child did not cause (observed live: a red
main rolled back every lane). ``verifiers/fallback_guard.py`` needs the same
surface, and two independent derivations would silently disagree — one gate
would pass the file the other failed.

Resolution order for the base:

1. the run dir's ``pre-implementer-ref`` (``$MINI_ORK_HOME/runs/<id>/``), the
   exact tree the implementer started from;
2. ``MO_GOAL_SCOPED_BASE`` for callers with no run dir;
3. ``merge-base HEAD origin/main`` (then ``main``) as the last resort.

Fail-open on git: no repository, no git binary, or a timeout yields ``[]``.
Callers that must distinguish "nothing changed" from "could not attribute" call
``is_git_worktree`` first and abstain — an empty list is not a pass.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import List

_HEX_REF = re.compile(r"[0-9a-fA-F]{7,40}")
#: Never part of a child's change surface.
_NOISE_PREFIX = (".mini-ork/",)


def _git(root: str, *args: str, timeout_s: int = 120) -> "tuple[int, str]":
    """Run git without ever raising. Returns ``(rc, stdout)``."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True,
            timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout or ""


def is_git_worktree(root: str) -> bool:
    rc, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out.strip() == "true"


def run_dir_ref() -> str:
    """This run's pre-implementer ref, when the run dir carries one."""
    home = os.environ.get("MINI_ORK_HOME", "").strip()
    run_id = os.environ.get("MINI_ORK_RUN_ID", "").strip()
    if not (home and run_id):
        return ""
    try:
        ref = Path(home, "runs", run_id, "pre-implementer-ref").read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""
    # A garbage ref must never reach git as a revision argument.
    return ref if _HEX_REF.fullmatch(ref) else ""


def _resolves(root: str, ref: str) -> bool:
    """True when ``ref`` names a commit in this repository.

    A base that does not resolve is worse than no base: the caller then diffs
    against nothing, and every pre-existing import or requirement in a touched
    file is indistinguishable from one the change added.
    """
    rc, _ = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return rc == 0


def scoped_base(root: str) -> str:
    """The base the diff is taken against — the run's own start point first."""
    ref = run_dir_ref() or os.environ.get("MO_GOAL_SCOPED_BASE", "").strip()
    if ref and _resolves(root, ref):
        return ref
    for candidate in ("origin/main", "main"):
        rc, out = _git(root, "merge-base", "HEAD", candidate)
        if rc == 0 and out.strip():
            return out.strip()
    return ""


def touched_files(root: str) -> List[str]:
    """Repo-relative paths this run changed: working tree, untracked, and
    (when a base resolves) everything committed on top of it."""
    paths = set()
    # ``-z`` is what makes a path with a space, a quote or a non-ASCII byte come
    # back verbatim. Without it git quotes such a path and C-escapes the content,
    # and stripping the quotes yields a name that matches no file — so the
    # surface a gate intersects against would silently omit the run's own file.
    rc, out = _git(root, "status", "--porcelain", "-z", "-uall")
    if rc == 0:
        records = out.split("\x00")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4:
                continue
            rel = record[3:]
            if record[0] in "RC":
                # A rename/copy is followed by its source path as its own
                # record; the destination is the one this run touched.
                index += 1
            rel = rel.strip()
            if rel:
                paths.add(rel)
    base = scoped_base(root)
    if base:
        # ``-z`` for the same reason as ``status`` above: ``--name-only`` without
        # it quotes a path holding a space or a non-ASCII byte, so a committed
        # file would vanish from the surface while an uncommitted one survived.
        rc, out = _git(root, "diff", "--name-only", "-z", base + "...HEAD")
        if rc == 0:
            paths.update(p.strip() for p in out.split("\x00") if p.strip())
    return sorted(p for p in paths if p and not p.startswith(_NOISE_PREFIX))
