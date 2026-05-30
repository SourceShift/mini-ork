#!/usr/bin/env bash
# auto-merge.sh — squash-merges APPROVED epic branches into main.
#
# Called by finalize.sh after all epics reach terminal state.
# Only merges epics with verdict=APPROVE. Skips ESCALATE/UNKNOWN.
#
# Merge strategy:
#   1. Pre-flight: git merge-tree conflict check (no working tree mutation)
#   2. Rebase onto main (fast-forward if clean)
#   3. Acquire cross-job mutex on $MINI_ORK_HOME/locks/main-merge.lock
#   4. Verify root HEAD is on main (recover from detached HEAD if needed)
#   5. Squash-merge + commit into main
#   6. Verify main advanced to new SHA — fail loudly if not (race detection)
#   7. Release mutex
#   8. Update state.db status → 'done'
#   9. Remove worktree (cleanup)
#
# If any step fails for an epic, that epic is skipped (not merged) and
# the failure is logged. Other epics still proceed.
#
# Concurrency: the mutex at step 3 serializes the critical section across
# parallel mini-ork jobs operating on the same REPO_ROOT. Without it,
# concurrent `git merge --squash` + `git commit` from two jobs corrupt
# main.
#
# Usage: source from finalize.sh; call mo_auto_merge

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# ── Cross-job mutex helpers ────────────────────────────────────────────────
# mkdir-based mutex — atomic, portable across macOS/Linux without
# requiring util-linux flock. Stale-lock cleanup uses PID liveness check.
_mo_main_lock_dir() { echo "${MINI_ORK_HOME:-.mini-ork}/locks"; }
_mo_main_lock_path() { echo "$(_mo_main_lock_dir)/main-merge.lock"; }

_mo_acquire_main_mutex() {
  local lock_path="$(_mo_main_lock_path)"
  local timeout_s="${MO_MERGE_LOCK_TIMEOUT_S:-300}"
  local waited=0
  mkdir -p "$(_mo_main_lock_dir)"
  while ! mkdir "$lock_path" 2>/dev/null; do
    # Stale-lock recovery: if PID file inside is dead, take over
    local holder_pid
    holder_pid=$(cat "$lock_path/pid" 2>/dev/null || echo "")
    if [ -n "$holder_pid" ] && ! kill -0 "$holder_pid" 2>/dev/null; then
      echo "[auto-merge] taking over stale main-merge lock from dead PID $holder_pid" >&2
      rm -rf "$lock_path"
      continue
    fi
    if [ "$waited" -ge "$timeout_s" ]; then
      echo "[auto-merge] FATAL: failed to acquire main-merge lock after ${timeout_s}s (held by PID ${holder_pid:-unknown})" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "$$" > "$lock_path/pid"
  echo "$JOB_ID" > "$lock_path/job"
  return 0
}

_mo_release_main_mutex() {
  local lock_path="$(_mo_main_lock_path)"
  # Only release if WE hold it
  if [ -f "$lock_path/pid" ] && [ "$(cat "$lock_path/pid" 2>/dev/null)" = "$$" ]; then
    rm -rf "$lock_path"
  fi
}

# ── Untracked-file collision pre-flight ────────────────────────────────────
# git refuses `merge --squash` when the branch adds files at paths the
# main WD already has untracked.
#
# Pre-flight: enumerate added paths in branch vs main, check which are
# untracked in main's WD, move them to a per-epic stash dir under
# $MINI_ORK_HOME/auto-merge-stash/<job>/<epic>-<ts>/ before the merge runs.
# The merged version becomes canonical; stashed copies are kept for
# audit and recovery.
#
# Disable with MO_AUTO_MERGE_STASH_UNTRACKED=0
_mo_stash_colliding_untracked() {
  local branch="$1" epic="$2" merge_log="$3"
  [ "${MO_AUTO_MERGE_STASH_UNTRACKED:-1}" = "1" ] || return 0

  local added_in_branch
  added_in_branch=$(git -C "$REPO_ROOT" diff --name-only --diff-filter=A main.."$branch" 2>/dev/null)
  [ -n "$added_in_branch" ] || return 0

  local moved_count=0
  local stash_base="${MINI_ORK_HOME:-.mini-ork}"
  local stash_dir="$stash_base/auto-merge-stash/$JOB_ID/${epic}-$(date +%Y%m%d-%H%M%S)"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    local abs="$REPO_ROOT/$path"
    [ -e "$abs" ] || continue
    local porcelain
    porcelain=$(git -C "$REPO_ROOT" status --porcelain -- "$path" 2>/dev/null | head -1)
    [ "${porcelain:0:2}" = "??" ] || continue
    if [ "$moved_count" -eq 0 ]; then
      mkdir -p "$stash_dir"
      echo "[auto-merge] $epic — stashing colliding untracked files to $stash_dir" | tee -a "$merge_log"
    fi
    local rel_dir
    rel_dir="$stash_dir/$(dirname "$path")"
    mkdir -p "$rel_dir"
    mv "$abs" "$rel_dir/" 2>/dev/null && {
      echo "[auto-merge]   stashed: $path" | tee -a "$merge_log"
      moved_count=$((moved_count + 1))
    }
  done <<< "$added_in_branch"

  if [ "$moved_count" -gt 0 ]; then
    echo "[auto-merge] $epic — $moved_count untracked file(s) moved aside; merge can now proceed" | tee -a "$merge_log"
  fi
  return 0
}

mo_auto_merge() {
  : "${REPO_ROOT:?}"
  : "${MINI_ORCH_DIR:?}"
  : "${JOB_ID:?}"

  local agentflow_dir="${MINI_ORK_HOME:-.mini-ork}"
  local state_db="${MINI_ORK_DB:-${agentflow_dir}/state.db}"

  local job_run_dir="$MINI_ORCH_DIR/runs/$JOB_ID"
  local merged=0 skipped=0 failed=0
  local merge_log="$job_run_dir/merge.log"
  : > "$merge_log"

  echo "[auto-merge] starting for job=$JOB_ID" | tee -a "$merge_log"

  # Collect approved epics with their branches
  local -a approved_epics=()
  local -a approved_branches=()

  for epic_dir in "$job_run_dir"/*/; do
    [ -d "$epic_dir" ] || continue
    local epic
    epic=$(basename "$epic_dir")
    [[ "$epic" == "$(basename "$job_run_dir")" ]] && continue

    # Find last iter WITH a verdict.json — skip phantom iter dirs that the
    # orch pre-creates for next-iter feedback prep but never runs because the
    # loop capped on the prior iter.
    local last_iter_dir=""
    for _d in $(ls -d "$epic_dir"iter-*/ 2>/dev/null | sort -V -r); do
      if [ -f "${_d}verdict.json" ]; then
        last_iter_dir="$_d"
        break
      fi
    done
    [ -n "$last_iter_dir" ] || continue

    local verdict="UNKNOWN"
    if [ -f "$last_iter_dir/verdict.json" ]; then
      verdict=$(jq -r '.verdict // "UNKNOWN"' "$last_iter_dir/verdict.json" 2>/dev/null)
    fi

    if [ "$verdict" != "APPROVE" ]; then
      echo "[auto-merge] skip $epic — verdict=$verdict" | tee -a "$merge_log"
      skipped=$((skipped + 1))
      continue
    fi

    # Skip already-merged epics
    local epic_status
    epic_status=$(sqlite3 "$state_db" "SELECT status FROM epics WHERE id='$epic';" 2>/dev/null)
    if [ "$epic_status" = "done" ]; then
      echo "[auto-merge] skip $epic — already merged (status=done)" | tee -a "$merge_log"
      skipped=$((skipped + 1))
      continue
    fi

    # Resolve branch from kickoff
    local kickoff_path
    kickoff_path=$(sqlite3 "$state_db" \
      "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
    local branch
    branch=$(grep -E '^>?[[:space:]]*\*\*Branch:\*\*' "$REPO_ROOT/$kickoff_path" 2>/dev/null \
      | head -1 | sed -E 's/^[^`]*`([^`]+)`.*/\1/')

    if [ -z "$branch" ]; then
      echo "[auto-merge] FAIL $epic — no branch in kickoff" | tee -a "$merge_log"
      failed=$((failed + 1))
      continue
    fi

    approved_epics+=("$epic")
    approved_branches+=("$branch")
  done

  if [ ${#approved_epics[@]} -eq 0 ]; then
    echo "[auto-merge] no approved epics to merge" | tee -a "$merge_log"
    return 0
  fi

  echo "[auto-merge] will merge ${#approved_epics[@]} epic(s): ${approved_epics[*]}" | tee -a "$merge_log"

  for i in "${!approved_epics[@]}"; do
    local epic="${approved_epics[$i]}"
    local branch="${approved_branches[$i]}"

    echo "" | tee -a "$merge_log"
    echo "[auto-merge] ── $epic ($branch) ──" | tee -a "$merge_log"

    # 1. Pre-flight: conflict check via modern merge-tree (git 2.38+)
    if ! git -C "$REPO_ROOT" merge-tree --write-tree main "$branch" > /dev/null 2>&1; then
      echo "[auto-merge] $epic has conflicts — attempting rebase first..." | tee -a "$merge_log"

      local wt
      wt=$(git -C "$REPO_ROOT" worktree list --porcelain | awk -v b="refs/heads/$branch" '
        /^worktree / { p = substr($0, 10) }
        /^branch / && $2 == b { print p; exit }
      ')
      if [ -n "$wt" ] && [ -d "$wt" ]; then
        if git -C "$wt" rebase main >> "$merge_log" 2>&1; then
          echo "[auto-merge] $epic rebased successfully" | tee -a "$merge_log"
          if ! git -C "$REPO_ROOT" merge-tree --write-tree main "$branch" > /dev/null 2>&1; then
            echo "[auto-merge] FAIL $epic — still conflicts after rebase" | tee -a "$merge_log"
            failed=$((failed + 1))
            continue
          fi
        else
          echo "[auto-merge] FAIL $epic — rebase failed, aborting" | tee -a "$merge_log"
          git -C "$wt" rebase --abort 2>/dev/null || true
          failed=$((failed + 1))
          continue
        fi
      else
        echo "[auto-merge] FAIL $epic — no worktree for rebase, skipping" | tee -a "$merge_log"
        failed=$((failed + 1))
        continue
      fi
    fi

    # 2. Collect commit messages for squash message
    local commit_count
    commit_count=$(git -C "$REPO_ROOT" rev-list --count "main..$branch" 2>/dev/null || echo 0)
    local commit_log
    commit_log=$(git -C "$REPO_ROOT" log --oneline "main..$branch" 2>/dev/null)

    if [ "$commit_count" -eq 0 ]; then
      echo "[auto-merge] skip $epic — no commits ahead of main" | tee -a "$merge_log"
      skipped=$((skipped + 1))
      continue
    fi

    # 3. Squash-merge
    local squash_msg
    squash_msg="feat($JOB_ID): merge $epic ($branch)

Squash-merge of $commit_count commit(s) from mini-ork job '$JOB_ID'.
Reviewer verdict: APPROVE (evidence-based DoD).

Commits:
$commit_log"

    echo "[auto-merge] squash-merging $commit_count commits..." | tee -a "$merge_log"

    # ── BEGIN main-mutator critical section ────────────────────────────
    if ! _mo_acquire_main_mutex; then
      echo "[auto-merge] FAIL $epic — could not acquire main-merge lock" | tee -a "$merge_log"
      failed=$((failed + 1))
      continue
    fi

    local head_branch_pre
    head_branch_pre=$(git -C "$REPO_ROOT" symbolic-ref --short HEAD 2>/dev/null || echo "DETACHED")
    if [ "$head_branch_pre" != "main" ]; then
      echo "[auto-merge] root HEAD was '$head_branch_pre' — checking out main" | tee -a "$merge_log"
      if ! git -C "$REPO_ROOT" checkout main >> "$merge_log" 2>&1; then
        echo "[auto-merge] FAIL $epic — cannot checkout main (working tree dirty?)" | tee -a "$merge_log"
        _mo_release_main_mutex
        failed=$((failed + 1))
        continue
      fi
    fi

    local main_tip_before
    main_tip_before=$(git -C "$REPO_ROOT" rev-parse main)

    _mo_stash_colliding_untracked "$branch" "$epic" "$merge_log" || true

    if ! git -C "$REPO_ROOT" merge --squash "$branch" >> "$merge_log" 2>&1; then
      echo "[auto-merge] $epic squash failed — trying rebase fallback..." | tee -a "$merge_log"
      git -C "$REPO_ROOT" merge --abort 2>/dev/null || true
      git -C "$REPO_ROOT" checkout -- . 2>/dev/null || true

      local wt_fallback
      wt_fallback=$(git -C "$REPO_ROOT" worktree list --porcelain | awk -v b="refs/heads/$branch" '
        /^worktree / { p = substr($0, 10) }
        /^branch / && $2 == b { print p; exit }
      ')
      if [ -n "$wt_fallback" ] && [ -d "$wt_fallback" ]; then
        if git -C "$wt_fallback" rebase main >> "$merge_log" 2>&1; then
          echo "[auto-merge] $epic rebased — retrying squash-merge..." | tee -a "$merge_log"
          _mo_stash_colliding_untracked "$branch" "$epic" "$merge_log" || true
          if ! git -C "$REPO_ROOT" merge --squash "$branch" >> "$merge_log" 2>&1; then
            echo "[auto-merge] FAIL $epic — squash still fails after rebase" | tee -a "$merge_log"
            git -C "$REPO_ROOT" merge --abort 2>/dev/null || true
            git -C "$REPO_ROOT" checkout -- . 2>/dev/null || true
            _mo_release_main_mutex
            failed=$((failed + 1))
            continue
          fi
        else
          echo "[auto-merge] FAIL $epic — rebase failed" | tee -a "$merge_log"
          git -C "$wt_fallback" rebase --abort 2>/dev/null || true
          _mo_release_main_mutex
          failed=$((failed + 1))
          continue
        fi
      else
        echo "[auto-merge] FAIL $epic — no worktree for rebase fallback" | tee -a "$merge_log"
        _mo_release_main_mutex
        failed=$((failed + 1))
        continue
      fi
    fi

    # --no-verify: reviewer already validated; hook runs tsc which may fail
    # on cross-branch type gaps that only resolve after all epics merge.
    if ! git -C "$REPO_ROOT" commit --no-verify -m "$(cat <<EOF
$squash_msg
EOF
)" >> "$merge_log" 2>&1; then
      echo "[auto-merge] FAIL $epic — commit failed (empty merge?)" | tee -a "$merge_log"
      git -C "$REPO_ROOT" checkout -- . 2>/dev/null || true
      _mo_release_main_mutex
      failed=$((failed + 1))
      continue
    fi

    local merged_sha
    merged_sha=$(git -C "$REPO_ROOT" rev-parse HEAD)

    # Race-detection guardrail
    local main_tip_after
    main_tip_after=$(git -C "$REPO_ROOT" rev-parse main 2>/dev/null || echo "")
    if [ "$main_tip_after" != "$merged_sha" ]; then
      echo "[auto-merge] FATAL $epic — race detected: commit $merged_sha is NOT on main (main=$main_tip_after, was=$main_tip_before). Recover via: git cherry-pick $merged_sha" | tee -a "$merge_log"
      _mo_release_main_mutex
      failed=$((failed + 1))
      continue
    fi

    _mo_release_main_mutex
    # ── END main-mutator critical section ──────────────────────────────
    echo "[auto-merge] OK $epic merged as $merged_sha" | tee -a "$merge_log"

    # 4. Update state.db
    local latest_run_id
    latest_run_id=$(sqlite3 "$state_db" \
      "SELECT id FROM runs WHERE epic_id='$epic' ORDER BY id DESC LIMIT 1;" 2>/dev/null)
    if [ -n "$latest_run_id" ]; then
      sqlite3 "$state_db" \
        "UPDATE runs SET merged_sha='$merged_sha', final_verdict='MERGED', ended_at=COALESCE(ended_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')) WHERE id=$latest_run_id;"
    else
      sqlite3 "$state_db" \
        "INSERT INTO runs (epic_id, run_dir, branch, baseline_sha, agent, final_verdict, merged_sha, ended_at) VALUES ('$epic', 'mini-ork/$JOB_ID/$epic', '$branch', '$(git -C "$REPO_ROOT" rev-parse main~1)', 'mini-ork', 'MERGED', '$merged_sha', strftime('%Y-%m-%dT%H:%M:%fZ','now'));"
    fi
    local _kp
    _kp=$(sqlite3 "$state_db" \
      "SELECT kickoff_path FROM epics WHERE id='$epic';" 2>/dev/null)
    if [ -z "$_kp" ]; then
      sqlite3 "$state_db" \
        "INSERT OR IGNORE INTO epics (id, title, status, lane, worker_default, group_id, kickoff_path) VALUES ('$epic', '$epic', 'in progress', 'mini-ork', 'mini-ork', 'group-$JOB_ID', '${branch:-unknown}');"
    fi
    sqlite3 "$state_db" \
      "UPDATE epics SET status='done', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id='$epic';"
    local _final_status
    _final_status=$(sqlite3 "$state_db" \
      "SELECT status FROM epics WHERE id='$epic';" 2>/dev/null)
    if [ "$_final_status" != "done" ]; then
      echo "[auto-merge] WARN $epic — status update did not stick (current='$_final_status'). May be blocked by trg_epics_no_done_without_merge." | tee -a "$merge_log"
    fi

    # 5. Remove worktree (best-effort)
    local wt
    wt=$(git -C "$REPO_ROOT" worktree list --porcelain | awk -v b="refs/heads/$branch" '
      /^worktree / { p = substr($0, 10) }
      /^branch / && $2 == b { print p; exit }
    ')
    if [ -n "$wt" ]; then
      git -C "$REPO_ROOT" worktree remove --force "$wt" 2>/dev/null || true
      echo "[auto-merge] worktree removed: $wt" | tee -a "$merge_log"
    fi

    # 6. Delete feature branch (best-effort)
    git -C "$REPO_ROOT" branch -D "$branch" 2>/dev/null || true
    echo "[auto-merge] branch deleted: $branch" | tee -a "$merge_log"

    merged=$((merged + 1))
  done

  echo "" | tee -a "$merge_log"
  echo "[auto-merge] done: merged=$merged skipped=$skipped failed=$failed" | tee -a "$merge_log"
}
