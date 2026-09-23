#!/usr/bin/env python3
"""Deploy a verified fix: branch + LOCAL commit + worker restart.

Binding-agnostic: the planning-wedge and book-chapter loops both use it, so the
commit subject comes from MO_GOAL_COMMIT_SUBJECT rather than being hardcoded here.

The blast radius the operator chose for this loop. The chapter loop's launcher
offers two modes — ``prod-push`` (ff to origin/main, ALL books, irreversible)
and ``local-worker`` (restart the worker from the worktree). This is a third,
narrower one for planning: the fix is COMMITTED on a branch in the worktree and
the local worker is restarted against it. Nothing is pushed. ``main`` stays
reviewable, and the only thing the fix can affect is the one worker it restarts.

Why commit before restarting, rather than restart from a dirty worktree:
  * A dirty restart makes the running code unattributable. Six hours later
    nobody can tell which bytes the worker was executing, and re-running the
    loop gives a different answer. A commit pins it.
  * It gives the human a reviewable artifact — the thing the operator is
    expected to read — without putting it anywhere shared.

STAGING IS NARROW ON PURPOSE. This never runs ``git add -A``. That sweeps
whatever else is lying around, including a concurrent session's staged files and
generated trees (this repo has bitten on that before: an ``add -A`` here would
pick up ``baml_client/``). Instead it stages exactly the paths git reports as
changed — tracked modifications plus new non-ignored files.

...and then filters those to the child's DECLARED SCOPE, which is the part a
plain ``--exclude-standard`` does not do. Measured 2026-09-21 in a freshly
created worktree: ``git ls-files --others --exclude-standard`` returns
``baml_client`` — the generated BAML tree is NOT gitignored in this repo, so
"non-ignored untracked" alone would stage the whole generated client into every
autonomous commit, alongside (and dwarfing) the actual fix. The child kickoff
declares ``server/`` as its only in-scope area, so the deploy commits only
changed paths under ``MO_GOAL_DEPLOY_SCOPE`` (default ``server``) and REPORTS
everything else rather than committing it. Out-of-scope changes are a warning,
not a refusal: a dirty worktree of unrelated files is normal and must not abort
a real deploy — but it must never be silently swept in either.

Ordering vs. the loop (``transforms.py::_run_apply``): this runs as
MO_GOAL_APPLY_CMD, then the loop awaits the deploy, then re-dispatches. So this
script must leave the new code LIVE when it returns — hence the restart is part
of it, not a separate step.

Contract: invoked as MO_GOAL_APPLY_CMD (shell, NO argv) inside
MO_GOAL_TARGET_CWD. Exit 0 == deployed (or nothing to do). Non-zero == the
deploy failed and the loop must not re-dispatch against stale code.
Set MO_GOAL_BRANCH_DEPLOY_DRY=1 to print the plan without committing/restarting.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

_DEFAULT_BRANCH = ""  # empty ⇒ commit on the worktree's current branch

# The quality bar's implementation, one layer below MO_GOAL_PROTECTED_PATHS
# (which the running driver froze at launch and this script cannot read
# reliably mid-run). Measured 2026-09-23 (d2b142b0c): a swept child experiment
# deleted these gates + their tests and the worker ran gate-less for hours.
_FROZEN_GATES = (
    "server/services/bookGeneration/chapterCommitGates.ts",
    "server/services/bookGeneration/figureRenderPhase.ts",
    "server/services/bookGeneration/planAdoptionGate.ts",
)


def _git(args: list[str], cwd: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def _changed_paths(cwd: str) -> list[str]:
    """Tracked modifications + new non-ignored files, deduped, order-stable.

    ``--exclude-standard`` is what makes including untracked files safe: it
    honours .gitignore, so build output cannot ride along — except for a
    generated tree this repo does NOT ignore (``baml_client``); see the module
    docstring. Scope filtering happens in :func:`_split_scope`.
    """
    out: list[str] = []
    seen: set[str] = set()
    for args in (
        ["diff", "--name-only", "HEAD"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        proc = _git(args, cwd)
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            path = line.strip()
            if path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _scope_prefix() -> str:
    prefix = os.environ.get("MO_GOAL_DEPLOY_SCOPE", "server").strip().strip("/")
    return f"{prefix}/" if prefix else ""


def _split_scope(paths: list[str]) -> tuple[list[str], list[str]]:
    """``(in_scope, out_of_scope)`` — the child kickoff declares ``server/`` only."""
    prefix = _scope_prefix()
    if not prefix:
        return paths, []
    in_scope = [p for p in paths if p == prefix.rstrip("/") or p.startswith(prefix)]
    out_scope = [p for p in paths if p not in set(in_scope)]
    return in_scope, out_scope


def _restore_frozen(cwd: str, paths: list[str], dry: bool) -> list[str]:
    """Restore (never sweep) frozen gate files and DELETED test files.

    Restore-from-HEAD rather than refuse-the-deploy: a refusal wedges the whole
    wave on one dirty frozen path, while a restore lets the legitimate part of
    the fix deploy and un-launders the bar in the LIVE tree too (the worker
    restarts from the working tree, so exclusion-from-staging alone would still
    run gutted gates).
    """
    deleted: set[str] = set()
    proc = _git(["diff", "--name-status", "HEAD"], cwd)
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip().startswith("D"):
                deleted.add(parts[-1].strip())
    frozen = [
        p for p in paths
        if p in _FROZEN_GATES or (p in deleted and "/__tests__/" in p)
    ]
    if frozen:
        print(f"branch+local deploy: {len(frozen)} FROZEN path(s) restored from "
              "HEAD — the bar is not a fix surface:")
        for p in frozen:
            print(f"    ! {p}")
        if not dry:
            restore = _git(["checkout", "HEAD", "--", *frozen], cwd)
            if restore.returncode != 0:
                print(f"frozen restore failed: {restore.stderr.strip()[:200]}",
                      file=sys.stderr)
    return [p for p in paths if p not in set(frozen)]


def main() -> int:
    cwd = os.environ.get("MO_GOAL_TARGET_CWD", "").strip()
    if not cwd or not os.path.isdir(os.path.join(cwd, ".git")) and not os.path.isdir(
        os.path.join(cwd, ".git")
    ):
        # .git may be a FILE in a worktree; just require the dir to exist.
        if not cwd or not os.path.isdir(cwd):
            print(f"MO_GOAL_TARGET_CWD unset/not a dir: {cwd!r}", file=sys.stderr)
            return 2

    dry = os.environ.get("MO_GOAL_BRANCH_DEPLOY_DRY", "").strip() == "1"
    paths = _changed_paths(cwd)
    paths, out_of_scope = _split_scope(paths)
    paths = _restore_frozen(cwd, paths, dry)

    branch_proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    current = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "?"
    target_branch = os.environ.get("MO_GOAL_FIX_BRANCH", _DEFAULT_BRANCH).strip()

    if out_of_scope:
        # Reported, never committed. A generated tree the repo does not ignore
        # (baml_client) or an unrelated edit from another session lands here.
        print(f"branch+local deploy: {len(out_of_scope)} changed path(s) OUTSIDE "
              f"{_scope_prefix()!r} — reported, NOT staged:")
        for p in out_of_scope[:10]:
            print(f"    ? {p}")
        if len(out_of_scope) > 10:
            print(f"    … and {len(out_of_scope) - 10} more")

    if not paths:
        # A clean tree is not a failure: the child may have correctly concluded
        # there was nothing to change (e.g. the wedge already cleared under
        # someone else's fix). Leave the restart to happen anyway so the loop's
        # await runs against a known-good worker rather than skipping ahead.
        print("branch+local deploy: no in-scope changed paths — nothing to commit")
        return _restart(cwd, dry)

    print(f"branch+local deploy: {len(paths)} in-scope changed path(s) on {current}")
    for p in paths[:40]:
        print(f"    M {p}")
    if len(paths) > 40:
        print(f"    … and {len(paths) - 40} more")

    if dry:
        print(f"    (dry) would commit on branch "
              f"{target_branch or current} and restart the worker")
        return 0

    if target_branch and target_branch != current:
        # Only switch when the operator named a branch explicitly; otherwise the
        # worktree's own branch IS the isolating branch.
        exists = _git(["rev-parse", "--verify", "--quiet", target_branch], cwd)
        if exists.returncode != 0:
            co = _git(["checkout", "-b", target_branch], cwd)
        else:
            co = _git(["checkout", target_branch], cwd)
        if co.returncode != 0:
            print(f"could not switch to {target_branch}: {co.stderr.strip()[:200]}",
                  file=sys.stderr)
            return 1
        current = target_branch

    add = _git(["add", "--", *paths], cwd)
    if add.returncode != 0:
        print(f"git add failed: {add.stderr.strip()[:200]}", file=sys.stderr)
        return 1

    goal = os.environ.get("MO_GOAL_GOAL_ID", "goal-loop")
    run_id = os.environ.get("MINI_ORK_RUN_ID", "")
    # Subject is binding-supplied: the two bindings that use this deploy repair
    # different failure classes (a wedged planning burst vs. a chapter that cannot
    # reach the quality bar), and the commit is the artifact a human reads.
    subject = os.environ.get("MO_GOAL_COMMIT_SUBJECT", "").strip() or (
        f"fix(compose): autonomous RSI repair ({goal})"
    )
    msg = (
        f"{subject}\n\n"
        "Produced by the goal-loop: a verified fix from an autonomous RSI wave.\n"
        "Committed locally on a branch in the worktree; not pushed.\n\n"
        f"goal: {goal}\nrun:  {run_id}\nts:   {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
    )
    commit = _git(["commit", "-m", msg], cwd, timeout=180)
    if commit.returncode != 0:
        print(f"git commit failed: "
              f"{(commit.stderr or commit.stdout).strip()[:300]}", file=sys.stderr)
        return 1
    head = _git(["rev-parse", "--short", "HEAD"], cwd).stdout.strip()
    print(f"committed {head} on {current} (LOCAL only — not pushed)")

    return _restart(cwd, dry)


def _restart(cwd: str, dry: bool) -> int:
    """Restart the local worker FROM the worktree so the new code goes live.

    Delegates to restart_worker.py, which owns the singleton-lock dance
    (SIGTERM the watchdog, not the leaf, or the watchdog respawns a leaf from
    the PRIMARY checkout and wins the lock). Reusing it keeps that knowledge in
    one place instead of forking a second copy.
    """
    binding = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(binding, "restart_worker.py")
    if not os.path.isfile(script):
        print(f"restart_worker.py not found at {script}", file=sys.stderr)
        return 1
    if dry:
        print(f"    (dry) would run python3 {script}")
        return 0
    proc = subprocess.run([sys.executable, script], cwd=cwd)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
