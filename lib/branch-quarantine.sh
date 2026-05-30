#!/usr/bin/env bash
# branch-quarantine.sh — reset contaminated worker branches before re-dispatch.
#
# When an epic ESCALATEs (or REQUEST_CHANGES with no progress), its worktree
# branch retains every iter's commits including the contract.sh auto-revert
# commits ("chore(mini-ork): auto-revert out-of-scope files (N files)").
# Re-dispatching the epic reuses the same worktree+branch, which means iter-1
# of the new run starts on top of contaminated state — corrupted by reverts
# the reviewer rejected, OR by partial-implementation commits the worker
# itself doesn't remember producing.
#
# This module detects contamination and resets the branch to its merge-base
# with main BEFORE the new iter's worker spawns. Uses git's reflog as the
# safety net so a misfire is recoverable.
#
# Citations:
#   AgentGit (arXiv:2511.00628)  — Git-like rollback for MAS workflows.
#   RAC      (arXiv:2605.03409)  — log-based recovery + compensation.
#
# Source from dispatch.sh / run.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Detect contamination: any commit between merge-base(main, HEAD) and HEAD
# whose subject matches the auto-revert signature.
#
# Args: worktree
# Returns:
#   0 — contamination detected (commit count printed on stdout)
#   1 — clean branch
mo_quarantine_detect() {
  local worktree="$1"
  [ -d "$worktree/.git" ] || [ -f "$worktree/.git" ] || return 1

  local base
  base=$(git -C "$worktree" merge-base main HEAD 2>/dev/null) || return 1
  [ -z "$base" ] && return 1

  # Look for auto-revert commits on the branch but not on main.
  local count
  count=$(git -C "$worktree" log "${base}..HEAD" --pretty=%s 2>/dev/null \
    | grep -cE '^chore\(mini-ork\): auto-revert out-of-scope' || true)
  [ -z "$count" ] && count=0

  if [ "$count" -gt 0 ]; then
    echo "$count"
    return 0
  fi
  return 1
}

# Reset the worktree's branch to its merge-base with main, preserving the
# original tip in a quarantine ref so it can be inspected/recovered.
#
# Args: epic worktree
# Returns:
#   0 — reset performed (or skipped via env), 1 — error.
mo_quarantine_reset() {
  local epic="$1" worktree="$2"

  [ "${MO_QUARANTINE_ON_AUTO_REVERT:-1}" -eq 1 ] || {
    echo "[mini-ork] $epic quarantine SKIPPED (MO_QUARANTINE_ON_AUTO_REVERT=0)" >&2
    return 0
  }

  # MOX-CONC H3: hard-reset wipes any uncommitted worker writes. Wait for
  # worker to quiesce (log mtime stable for 5s) before resetting. If worker
  # is still actively writing past max_wait, ABORT — quarantine is reactive,
  # never urgent enough to clobber live work.
  if declare -F mo_wait_for_worker_quiescence >/dev/null 2>&1 && declare -F mo_run_dir >/dev/null 2>&1; then
    if ! mo_wait_for_worker_quiescence "$(mo_run_dir "$epic")"; then
      echo "[mini-ork] $epic quarantine ABORTED — worker still active (try again after worker exits)" >&2
      return 1
    fi
  fi

  # Refuse if worktree is dirty — caller must commit/stash first.
  if [ -n "$(git -C "$worktree" status --porcelain --untracked-files=no | head -1)" ]; then
    echo "[mini-ork] $epic quarantine ABORTED — worktree dirty (commit/stash first)" >&2
    return 1
  fi

  local branch tip base
  branch=$(git -C "$worktree" symbolic-ref --short HEAD 2>/dev/null) || branch="HEAD"
  tip=$(git -C "$worktree" rev-parse HEAD 2>/dev/null)
  base=$(git -C "$worktree" merge-base main HEAD 2>/dev/null)
  [ -z "$tip" ] || [ -z "$base" ] && return 1
  [ "$tip" = "$base" ] && {
    echo "[mini-ork] $epic quarantine NO-OP — already at merge-base" >&2
    return 0
  }

  # Stash the contaminated tip in a refs/quarantine/<epic>/<ts> ref so it
  # can be resurrected with `git checkout refs/quarantine/<epic>/<ts>`.
  local ts ref
  ts=$(date -u +%Y%m%dT%H%M%S)
  ref="refs/quarantine/$epic/$ts"
  git -C "$worktree" update-ref "$ref" "$tip" 2>/dev/null || true

  echo "[mini-ork] $epic quarantine RESET branch=$branch tip=${tip:0:8} → base=${base:0:8} (preserved at $ref)" >&2
  if ! git -C "$worktree" reset --hard "$base" >/dev/null 2>&1; then
    echo "[mini-ork] $epic quarantine FAILED — reset aborted" >&2
    return 1
  fi

  # Audit trail: write a quarantine-decision.json next to the run dir if
  # mo_run_dir helper is available (sourced from dispatch.sh elsewhere).
  if declare -F mo_run_dir >/dev/null 2>&1; then
    local rd
    rd=$(mo_run_dir "$epic" 2>/dev/null || true)
    if [ -n "$rd" ]; then
      mkdir -p "$rd"
      jq -n \
        --arg epic "$epic" \
        --arg branch "$branch" \
        --arg from "$tip" \
        --arg to "$base" \
        --arg ref "$ref" \
        --arg ts "$ts" \
        '{epic:$epic, branch:$branch, from:$from, to:$to, preserved_ref:$ref, ts:$ts, action:"reset_to_merge_base"}' \
        > "$rd/quarantine-decision.json" 2>/dev/null || true
    fi
  fi
  return 0
}

# High-level wrapper: detect + reset in one call. Wired into dispatch
# loops before the iter-1 worker spawn.
#
# Args: epic worktree
# Returns: 0 always (errors are logged but don't block dispatch).
mo_quarantine_check_and_reset() {
  local epic="$1" worktree="$2"
  local found
  if found=$(mo_quarantine_detect "$worktree"); then
    echo "[mini-ork] $epic quarantine DETECTED $found auto-revert commit(s) on branch — resetting" >&2
    mo_quarantine_reset "$epic" "$worktree" || true
  fi
  return 0
}
