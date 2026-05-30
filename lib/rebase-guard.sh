#!/usr/bin/env bash
# mini-ork rebase-guard — keep worker branches in sync with main HEAD.
#
# Two firing points (both call mo_rebase_branch_onto_main):
#   1. Dispatch start — main may have advanced since worktree was scaffolded.
#   2. Mid-run, AFTER worker commits but BEFORE reviewer fires — main may have
#      advanced DURING the worker run. Without this, reviewer's
#      `git diff main..HEAD` shows main's new commits as missing-from-branch,
#      which it may interpret as worker scope violations.
#
# Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Rebase a worktree's current branch onto main HEAD if main has moved.
# Args: epic worktree [phase]   phase: "dispatch" | "mid-run" (label only)
# Returns:
#   0 — no rebase needed OR rebase clean
#   1 — rebase had conflicts (rebase aborted, branch unchanged)
#   2 — worktree dirty, skipped (caller must commit/stash before retry)
mo_rebase_branch_onto_main() {
  local epic="$1" worktree="$2" phase="${3:-dispatch}"

  [ "${MO_SKIP_AUTOREBASE:-0}" -eq 1 ] && return 0

  # `--untracked-files=no` excludes untracked entries from the "dirty" check
  # so freshly-scaffolded worktrees with symlinks or untracked config don't
  # trigger a premature skip.
  if [ -n "$(git -C "$worktree" status --porcelain --untracked-files=no | head -1)" ]; then
    echo "[mini-ork] $epic ($phase) worktree has uncommitted tracked changes — skipping rebase (commit/stash first)" >&2
    return 2
  fi

  local main_sha base_sha
  main_sha=$(git -C "$REPO_ROOT" rev-parse main 2>/dev/null)
  base_sha=$(git -C "$worktree" merge-base main HEAD 2>/dev/null)
  [ -z "$main_sha" ] || [ -z "$base_sha" ] && return 0
  [ "$main_sha" = "$base_sha" ] && return 0   # branch is fresh

  local behind
  behind=$(git -C "$worktree" log --oneline "${base_sha}..main" 2>/dev/null | wc -l | tr -d ' ')
  echo "[mini-ork] $epic ($phase) main moved $behind commit(s) since branch base — rebasing..." >&2

  # Overlap probe: if main's new commits touch zero files the branch
  # also touched, the rebase is guaranteed clean → auto-resolve.
  local branch_files main_files overlap_files
  branch_files=$(git -C "$worktree" diff --name-only main...HEAD 2>/dev/null | sort -u || true)
  main_files=$(git -C "$worktree" diff --name-only "${base_sha}..main" 2>/dev/null | sort -u || true)

  if [ -n "$branch_files" ] && [ -n "$main_files" ]; then
    overlap_files=$(printf '%s\n' "$branch_files" | grep -Fxf <(printf '%s\n' "$main_files") | sort -u || true)
  else
    overlap_files=""
  fi

  local decision_path
  decision_path="$(mo_run_dir "$epic")/rebase-decision.json"
  mkdir -p "$(dirname "$decision_path")"

  local decision

  if [ -z "$overlap_files" ]; then
    echo "[mini-ork] $epic ($phase) rebase NO-OVERLAP — auto-resolve" >&2
    git -C "$worktree" rebase main >/dev/null 2>&1
    decision="no_overlap_auto"
    _mo_write_rebase_decision "$decision_path" "$branch_files" "$main_files" "$overlap_files" "$decision"
    echo "[mini-ork] $epic ($phase) rebase clean (no-overlap) → new HEAD: $(git -C "$worktree" rev-parse --short HEAD)" >&2
    return 0
  fi

  echo "[mini-ork] $epic ($phase) rebase OVERLAP on: $(printf '%s ' $overlap_files)" >&2

  if git -C "$worktree" rebase main >/dev/null 2>&1; then
    echo "[mini-ork] $epic ($phase) rebase clean → new HEAD: $(git -C "$worktree" rev-parse --short HEAD)" >&2
    decision="overlap_attempted"
  else
    echo "[mini-ork] $epic ($phase) rebase CONFLICT — aborting; reviewer will see stale-base note" >&2
    git -C "$worktree" rebase --abort 2>/dev/null
    decision="conflict_aborted"
  fi

  _mo_write_rebase_decision "$decision_path" "$branch_files" "$main_files" "$overlap_files" "$decision"

  if [ "$decision" = "conflict_aborted" ]; then
    return 1
  fi
  return 0
}

# Helper: write structured rebase decision JSON.
# Args: path branch_files main_files overlap_files decision
_mo_write_rebase_decision() {
  local path="$1" branch_files="$2" main_files="$3" overlap_files="$4" decision="$5"
  local branch_files_arr main_files_arr overlap_files_arr
  branch_files_arr=$(printf '%s\n' "$branch_files" | jq -R 'select(length>0)' | jq -s . || echo '[]')
  main_files_arr=$(printf '%s\n' "$main_files" | jq -R 'select(length>0)' | jq -s . || echo '[]')
  overlap_files_arr=$(printf '%s\n' "$overlap_files" | jq -R 'select(length>0)' | jq -s . || echo '[]')
  jq -n \
    --argjson branch_files "$branch_files_arr" \
    --argjson main_files "$main_files_arr" \
    --argjson overlap_files "$overlap_files_arr" \
    --arg decision "$decision" \
    '{"branch_files": $branch_files, "main_files": $main_files, "overlap_files": $overlap_files, "decision": $decision}' > "$path"
}

# Mid-run guard: write a marker file the reviewer prompt can include.
# Args: epic iter
mo_write_stale_base_marker() {
  local epic="$1" iter="$2"
  local iter_dir="$(mo_run_dir "$epic")/iter-$iter"
  mkdir -p "$iter_dir"
  cat > "$iter_dir/stale-base.note" <<EOF
Worker branch could not be cleanly rebased onto current main HEAD.
Reviewer: when comparing diff against main, treat upstream changes
(commits on main not on branch) as out-of-scope context, NOT as
worker scope violations. Flag only changes the worker actively introduced
on this branch, not changes the worker is "missing" from main.
EOF
}
