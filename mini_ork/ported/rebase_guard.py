"""Python port of lib/rebase-guard.sh — keep worker branches in sync with main.

Strangler-fig parity port of ``mo_rebase_branch_onto_main`` + its decision
writer and stale-base marker. Git operations are shelled out (the function IS a
git-ops routine); the ported logic is the overlap probe + decision
categorisation, which the parity test exercises against real temp git repos.

    rebase_branch_onto_main(epic, worktree, phase="dispatch", *, repo_root, run_dir)
        -> 0 no rebase needed / rebase clean
           1 rebase had conflicts (aborted, branch unchanged)
           2 worktree dirty (tracked), skipped

The decision JSON ({branch_files, main_files, overlap_files, decision}) is
written to ``<run_dir>/rebase-decision.json`` exactly as the bash jq pipeline
produces it (sorted-unique file lists, one of the four decision strings).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _git(cwd, *args) -> tuple[str, int]:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def _name_list(cwd, *diff_args) -> list[str]:
    out, rc = _git(cwd, "diff", "--name-only", *diff_args)
    if rc != 0 or not out:
        return []
    return sorted(set(out.splitlines()))


def _write_rebase_decision(path, branch_files, main_files, overlap_files, decision) -> None:
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "branch_files": branch_files,
            "main_files": main_files,
            "overlap_files": overlap_files,
            "decision": decision,
        }, f)


def rebase_branch_onto_main(epic: str, worktree: str, phase: str = "dispatch", *,
                            repo_root: str, run_dir: str) -> int:
    """Rebase a worktree's current branch onto main HEAD if main has moved.

    ``run_dir`` is the equivalent of bash's ``mo_run_dir "$epic"`` (the
    decision JSON lands at ``<run_dir>/rebase-decision.json``).
    """
    if os.environ.get("MO_SKIP_AUTOREBASE", "0") == "1":
        return 0

    # tracked-only dirty check (untracked scaffolding must not trigger a skip)
    porcelain, _ = _git(worktree, "status", "--porcelain", "--untracked-files=no")
    if porcelain.splitlines()[:1]:
        return 2

    main_sha, _ = _git(repo_root, "rev-parse", "main")
    base_sha, _ = _git(worktree, "merge-base", "main", "HEAD")
    if not main_sha or not base_sha:
        return 0
    if main_sha == base_sha:
        return 0   # branch is fresh

    branch_files = _name_list(worktree, "main...HEAD")
    main_files = _name_list(worktree, f"{base_sha}..main")
    overlap_files = sorted(set(branch_files) & set(main_files)) if (branch_files and main_files) else []

    decision_path = os.path.join(run_dir, "rebase-decision.json")

    if not overlap_files:
        _git(worktree, "rebase", "main")
        _write_rebase_decision(decision_path, branch_files, main_files, overlap_files,
                               "no_overlap_auto")
        return 0

    _, rc = _git(worktree, "rebase", "main")
    if rc == 0:
        decision = "overlap_attempted"
    else:
        _git(worktree, "rebase", "--abort")
        decision = "conflict_aborted"
    _write_rebase_decision(decision_path, branch_files, main_files, overlap_files, decision)
    return 1 if decision == "conflict_aborted" else 0


_STALE_BASE_NOTE = """Worker branch could not be cleanly rebased onto current main HEAD.
Reviewer: when comparing diff against main, treat upstream changes
(commits on main not on branch) as out-of-scope context, NOT as
worker scope violations. Flag only changes the worker actively introduced
on this branch, not changes the worker is "missing" from main.
"""


def write_stale_base_marker(epic: str, iteration, *, run_dir: str) -> str:
    """Mirror mo_write_stale_base_marker: write the reviewer note. Returns path."""
    iter_dir = os.path.join(run_dir, f"iter-{iteration}")
    Path(iter_dir).mkdir(parents=True, exist_ok=True)
    note_path = os.path.join(iter_dir, "stale-base.note")
    with open(note_path, "w") as f:
        f.write(_STALE_BASE_NOTE)
    return note_path
