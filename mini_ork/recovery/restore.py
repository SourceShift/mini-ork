"""Restore a carry patch to the target tree before reusing an implementer checkpoint.

Why this module exists (kickoff §4 / lens §4):

A run that failed late — e.g. its live smoke check could not reach a surface —
already wrote a valid ``implementer-summary.json`` checkpoint, but the rollback
node reverted the working tree (see ``mini_ork/cli/execute_handlers.py:_revert_inplace_diff``
at :1380 and ``_record_rolled_back`` at :1290 which writes ``rolled-back.json``).
Re-running the closure on a clean tree would verify **pre-edit** code and silently
green-light a stale deliverable. ``restore_carry_patch`` reapplies the salvage
patch BEFORE dispatch so the reused verifier checks the new code.

This module is read-only on the run-dir side and a thin ``git apply`` wrapper on
the target side. It never calls into the LLM lane, never opens the E1
checkpoints table, and never modifies the engine repo. The CLI hooks
``restore_carry_patch`` for verify / resume / retry whenever the reuse set
contains an implementer-typed node and the run was rolled back; the ``--status``
path calls ``plan_restore`` (the read-only twin) so the operator can see what
*would* happen without touching the tree.

Public API:

    plan_restore(run_dir, *, cli_carry_patch=None, workflow_path=None) -> dict
        Read-only: returns the resolved target/patch and ``would_apply`` status.
        Never raises; missing artifacts degrade to ``would_apply="no_*"`` so the
        CLI's ``--status`` print stays coherent.

    restore_carry_patch(run_dir, target=None, *, cli_carry_patch=None,
                        workflow_path=None, dry_run=False) -> (status, message)
        Apply the resolved carry patch to the target tree. Returns
        ``(status, message)``; status in
        ``{"applied","already_applied","conflict","no_target","no_patch",
           "not_rolled_back"}``. Refuses on missing target/patch with a
        human-readable message — operators want a refusal reason on stderr,
        not a traceback.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

__all__ = ["restore_carry_patch", "plan_restore"]


# ─────────────────────────────────────────────────────────────────────────────
# Target / patch resolution
# ─────────────────────────────────────────────────────────────────────────────


def _target_from_run_dir(run_dir: str) -> Optional[str]:
    """Resolve the worktree the salvage patch should land in.

    Precedence (mirrors ``_salvage_before_revert`` at
    ``mini_ork/cli/execute_handlers.py:1579-1587`` so the two paths agree):

      1. ``implementer-summary.json`` ``worktree_path`` — the tree the
         original implementer edited (written by
         ``mini_ork/cli/execute.py:1885``, ``:1976``).
      2. ``run_profile.json`` ``roots.exec_cwd`` (null when the operator
         left it implicit) then ``roots.target`` — the run profile's
         intended cwd.

    Returns ``None`` when neither is set — caller MUST refuse.
    """
    if not run_dir:
        return None
    summary = os.path.join(run_dir, "implementer-summary.json")
    if os.path.isfile(summary):
        try:
            with open(summary, encoding="utf-8") as fh:
                data = json.load(fh)
            wt = data.get("worktree_path")
            if isinstance(wt, str) and wt and os.path.isdir(wt):
                return wt
        except (OSError, ValueError, AttributeError):
            pass
    profile = os.path.join(run_dir, "run_profile.json")
    if os.path.isfile(profile):
        try:
            with open(profile, encoding="utf-8") as fh:
                data = json.load(fh)
            roots = data.get("roots") or {}
            cwd = roots.get("exec_cwd") or roots.get("target")
            if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
                return cwd
        except (OSError, ValueError, AttributeError):
            pass
    return None


def _resolve_patch_path(
    run_dir: str,
    cli_carry_patch: str | None,
    workflow_path: str | None,
) -> Optional[str]:
    """Resolve the patch to run: ``--carry-patch`` > workflow > ``salvage.patch``.

    Precedence (kickoff §4):
      1. ``--carry-patch <name>`` if given (relative paths are run-dir relative
         so an operator can type ``cycle-delta.patch``); an absolute path is
         honored verbatim.
      2. Workflow top-level ``recovery: {carry_patch: <name>}`` if set.
      3. ``<run_dir>/salvage.patch`` — the default from
         ``mini_ork/cli/execute_handlers.py:_salvage_before_revert`` (line 1658).

    Returns the resolved path or ``None`` when none exist (caller refuses with
    ``"no_patch"``).
    """
    if cli_carry_patch:
        if os.path.isabs(cli_carry_patch):
            return cli_carry_patch if os.path.isfile(cli_carry_patch) else None
        candidate = os.path.join(run_dir, cli_carry_patch)
        return candidate if os.path.isfile(candidate) else None
    if workflow_path and os.path.isfile(workflow_path):
        try:
            import yaml  # type: ignore[import-untyped]

            with open(workflow_path, encoding="utf-8") as fh:
                wf = yaml.safe_load(fh) or {}
            rec = wf.get("recovery") or {}
            name = rec.get("carry_patch")
            if isinstance(name, str) and name:
                candidate = os.path.join(run_dir, name)
                if os.path.isfile(candidate):
                    return candidate
        except (OSError, ValueError, ImportError):
            pass
    candidate = os.path.join(run_dir, "salvage.patch")
    return candidate if os.path.isfile(candidate) else None


def _rolled_back_marker(run_dir: str) -> bool:
    """A ``rolled-back.json`` or ``salvage.patch`` is the signal that the
    implementer's tree was reverted and needs restoration.

    Either artifact is sufficient on its own — ``rolled-back.json`` is written
    by ``_record_rolled_back`` at ``execute_handlers.py:1290`` and
    ``salvage.patch`` by ``_salvage_before_revert`` at ``execute_handlers.py:1658``.
    """
    if not run_dir:
        return False
    return (os.path.isfile(os.path.join(run_dir, "rolled-back.json"))
            or os.path.isfile(os.path.join(run_dir, "salvage.patch")))


def _git(cwd: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run ``git -C cwd <args>`` and capture stderr. Mirrors
    ``mini_ork/workspaces.py:_git`` (:63) so the subprocess call shape stays
    consistent across the tree."""
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, text=True, check=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def plan_restore(
    run_dir: str,
    *,
    cli_carry_patch: str | None = None,
    workflow_path: str | None = None,
) -> dict:
    """Read-only plan: returns the resolved target/patch and ``would_apply``.

    Status transitions (the dry-run result the operator sees on ``--status``):

      * ``no_target``       — neither ``implementer-summary.json`` nor the
        run profile gives a usable target cwd.
      * ``no_patch``        — no patch found at the resolved path.
      * ``not_rolled_back`` — neither ``rolled-back.json`` nor ``salvage.patch``
        is present; no restore is necessary.
      * ``already_applied`` — ``git apply --check -R`` succeeds (the patch is
        the inverse of the current diff).
      * ``applied``         — ``git apply --3way --check`` succeeds; would
        apply cleanly.
      * ``conflict``         — ``git apply --3way --check`` fails; ``stderr``
        carries git's diagnostic.

    Always returns a dict — never raises on missing files or malformed JSON.
    """
    target = _target_from_run_dir(run_dir)
    patch = _resolve_patch_path(run_dir, cli_carry_patch, workflow_path)
    out = {
        "target": target,
        "patch": patch,
        "rolled_back": _rolled_back_marker(run_dir),
        "would_apply": None,
        "stderr": "",
    }
    if target is None:
        out["would_apply"] = "no_target"
        return out
    if not patch or not os.path.isfile(patch):
        out["would_apply"] = "no_patch"
        return out
    if not _rolled_back_marker(run_dir):
        out["would_apply"] = "not_rolled_back"
        return out
    rev = _git(target, ["apply", "--check", "-R", str(patch)])
    if rev.returncode == 0:
        out["would_apply"] = "already_applied"
        return out
    fwd = _git(target, ["apply", "--3way", "--check", str(patch)])
    if fwd.returncode == 0:
        out["would_apply"] = "applied"
    else:
        out["would_apply"] = "conflict"
        out["stderr"] = fwd.stderr.strip()
    return out


def restore_carry_patch(
    run_dir: str,
    target: str | None = None,
    *,
    cli_carry_patch: str | None = None,
    workflow_path: str | None = None,
    dry_run: bool = False,
) -> tuple[str, str]:
    """Apply the carry patch to the target tree.

    Refusal semantics: never raises on missing target / patch / conflict;
    the operator wants a refusal reason on stderr, not a traceback. The CLI
    pairs the returned ``status`` with ``message`` and decides.

    When the caller passes ``target`` explicitly (CLI hook in
    ``mini_ork/recovery/planner.py:_needs_restore`` already resolved it),
    we skip ``plan_restore``'s target probe and use the supplied path
    directly. The patch + rolled-back probe still go through the same
    file checks so a missing patch / not-rolled-back scenario refuses
    cleanly.

    ``dry_run=True`` returns ``("applied", "(dry-run) ..." )`` after the
    forward ``git apply --check`` succeeds; no mutation happens.
    """
    patch = _resolve_patch_path(run_dir, cli_carry_patch, workflow_path)
    target_resolved = target if target else _target_from_run_dir(run_dir)
    if not target_resolved:
        return ("no_target", "no implementer-summary.json worktree_path and no run_profile roots")
    if not patch or not os.path.isfile(patch):
        return ("no_patch", f"no carry patch at {patch or '<unresolved>'}")
    if not _rolled_back_marker(run_dir):
        return ("not_rolled_back", "no rolled-back.json / salvage.patch present — nothing to restore")
    rev = _git(target_resolved, ["apply", "--check", "-R", str(patch)])
    if rev.returncode == 0:
        return ("already_applied", f"carry patch already applied at {target_resolved}")
    fwd = _git(target_resolved, ["apply", "--3way", "--check", str(patch)])
    if fwd.returncode != 0:
        return ("conflict", f"git apply --3way --check failed: {fwd.stderr.strip()}")
    if dry_run:
        return ("applied", f"(dry-run) would apply {patch} to {target_resolved}")
    res = _git(target_resolved, ["apply", "--3way", str(patch)])
    if res.returncode != 0:
        return ("conflict", f"git apply --3way failed: {res.stderr.strip()}")
    return ("applied", f"applied {patch} to {target_resolved}")