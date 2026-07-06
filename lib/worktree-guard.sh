#!/usr/bin/env bash
# worktree-guard.sh — gate worktree mutations on worker liveness.
#
# MOX-CONC H2 + H3: contract.sh (scope-revert) and branch-quarantine.sh
# both mutate the worktree (`git checkout main -- $f`, `git reset --hard`)
# without checking whether a worker is mid-write. Silent data loss results
# when the worker's edits get overwritten / wiped while still in-flight.
#
# This module exposes ONE helper:
#
#   mo_wait_for_worker_quiescence <epic_run_dir> [max_wait_s] [stable_window_s]
#
# Returns 0 when:
#   - no iter-N/worker.pid file exists, OR
#   - the pid is dead, OR
#   - the worker.log mtime hasn't moved for `stable_window_s` seconds
# Returns 1 if the worker stays alive AND is actively writing past
# `max_wait_s` — caller decides whether to defer or proceed.
#
# Defaults: max_wait=30s, stable_window=5s. Tunable via env:
#   MO_WORKTREE_GUARD_MAX_WAIT_S   (default 30)
#   MO_WORKTREE_GUARD_STABLE_S     (default 5)

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

mo_wait_for_worker_quiescence() {
  local epic_run_dir="$1"
  local max_wait="${2:-${MO_WORKTREE_GUARD_MAX_WAIT_S:-30}}"
  local stable_window="${3:-${MO_WORKTREE_GUARD_STABLE_S:-5}}"

  [ -d "$epic_run_dir" ] || return 0  # no run dir = no worker = safe

  # Find the latest iter-N/worker.pid (the live one if any)
  local latest_pid_file
  latest_pid_file=$(ls -1t "$epic_run_dir"/iter-*/worker.pid 2>/dev/null | head -1)
  [ -z "$latest_pid_file" ] && return 0  # no worker pid = safe

  local worker_pid worker_log
  worker_pid=$(cat "$latest_pid_file" 2>/dev/null)
  worker_log="${latest_pid_file%/worker.pid}/worker.log"

  # No PID written or process dead → safe
  [ -z "$worker_pid" ] && return 0
  if ! kill -0 "$worker_pid" 2>/dev/null; then
    return 0
  fi

  # Worker alive — wait for log mtime to stabilize (no new writes for
  # stable_window seconds) OR worker to exit, OR max_wait to elapse.
  local elapsed=0 last_mtime="" cur_mtime stable_for=0
  while [ "$elapsed" -lt "$max_wait" ]; do
    if ! kill -0 "$worker_pid" 2>/dev/null; then
      return 0  # worker exited mid-wait
    fi
    if [ -f "$worker_log" ]; then
      cur_mtime=$(stat -c %Y "$worker_log" 2>/dev/null || stat -f %m "$worker_log" 2>/dev/null || echo 0)
      case "$cur_mtime" in ''|*[!0-9]*) cur_mtime=0 ;; esac
    else
      cur_mtime=0
    fi
    if [ "$cur_mtime" = "$last_mtime" ]; then
      stable_for=$((stable_for + 1))
      [ "$stable_for" -ge "$stable_window" ] && return 0
    else
      stable_for=0
      last_mtime="$cur_mtime"
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  # Still alive + still writing past max_wait. Caller decides.
  echo "[worktree-guard] worker pid=$worker_pid still active after ${max_wait}s — caller to decide" >&2
  return 1
}
