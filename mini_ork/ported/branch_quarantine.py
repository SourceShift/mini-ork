"""Python port of lib/branch-quarantine.sh — reset contaminated worker branches
before re-dispatch.

Strangler-fig parity port of the detect / reset / check-and-reset trio. Git
operations shell out (mirroring bash); the ported logic is the auto-revert
contamination probe, the reset-to-merge-base with a preserved quarantine ref,
and the audit-JSON. ``ts`` (bash's ``date -u +%Y%m%dT%H%M%S``) is injected so a
parity test can pin it; ``run_dir`` mirrors ``mo_run_dir "$epic"``.

    quarantine_detect(worktree)                 -> auto-revert commit count (0 = clean)
    quarantine_reset(epic, worktree, ts=…, …)   -> 0 reset/skip/no-op, 1 error/abort
    quarantine_check_and_reset(epic, worktree,…)-> 0 always
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _git(cwd, *args) -> tuple[str, int]:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def quarantine_detect(worktree: str) -> int:
    """Count auto-revert commits between merge-base(main,HEAD) and HEAD.
    Returns the count (0 when the branch is clean or not a git worktree)."""
    dotgit = Path(worktree) / ".git"
    if not (dotgit.is_dir() or dotgit.is_file()):
        return 0
    base, rc = _git(worktree, "merge-base", "main", "HEAD")
    if rc != 0 or not base:
        return 0
    subjects, rc = _git(worktree, "log", f"{base}..HEAD", "--pretty=%s")
    if rc != 0:
        return 0
    return sum(1 for s in subjects.splitlines()
               if s.startswith("chore(mini-ork): auto-revert out-of-scope"))


def quarantine_reset(epic: str, worktree: str, *, ts: str, run_dir: str | None = None,
                     wait_quiescence=None) -> int:
    """Reset the worktree branch to merge-base(main,HEAD), preserving the tip at
    refs/quarantine/<epic>/<ts>. Returns 0 (reset/skip/no-op), 1 (error/abort)."""
    if os.environ.get("MO_QUARANTINE_ON_AUTO_REVERT", "1") != "1":
        return 0  # skipped via env

    # Optional worker-quiescence gate (bash: declare -F mo_wait_for_worker_quiescence).
    if wait_quiescence is not None and run_dir is not None:
        if not wait_quiescence(run_dir):
            return 1  # worker still active — abort

    porcelain, _ = _git(worktree, "status", "--porcelain", "--untracked-files=no")
    if porcelain.splitlines()[:1]:
        return 1  # dirty — abort

    branch, rc = _git(worktree, "symbolic-ref", "--short", "HEAD")
    if rc != 0 or not branch:
        branch = "HEAD"
    tip, _ = _git(worktree, "rev-parse", "HEAD")
    base, _ = _git(worktree, "merge-base", "main", "HEAD")
    if not tip or not base:
        return 1
    if tip == base:
        return 0  # already at merge-base — no-op

    ref = f"refs/quarantine/{epic}/{ts}"
    _git(worktree, "update-ref", ref, tip)

    _, rc = _git(worktree, "reset", "--hard", base)
    if rc != 0:
        return 1

    if run_dir:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        with open(os.path.join(run_dir, "quarantine-decision.json"), "w") as f:
            json.dump({"epic": epic, "branch": branch, "from": tip, "to": base,
                       "preserved_ref": ref, "ts": ts, "action": "reset_to_merge_base"}, f)
    return 0


def quarantine_check_and_reset(epic: str, worktree: str, *, ts: str,
                               run_dir: str | None = None, wait_quiescence=None) -> int:
    """detect + reset in one call; always returns 0 (errors logged, non-blocking)."""
    if quarantine_detect(worktree) > 0:
        quarantine_reset(epic, worktree, ts=ts, run_dir=run_dir, wait_quiescence=wait_quiescence)
    return 0
