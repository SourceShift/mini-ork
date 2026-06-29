#!/usr/bin/env bash
# steering_checkpoint.sh — HITL steering checkpoints (U3).
#
# A checkpoint lets a recipe pause an in-flight run for human steering, then
# resume once steering arrives. It composes two primitives that already exist:
#   - operator_steering (lib/operator_steering.sh + the HTTP /steer endpoint)
#     carries the steering message,
#   - the dispatcher's plan-status gate (bin/mini-ork-execute) does the
#     pause/resume, exactly like plan_status=needs_answers.
#
# A recipe signals a checkpoint by emitting plan_status=needs_steering in its
# run_profile/plan. The gate then asks this library "is steering present yet?":
#   - YES  → clear the checkpoint marker, proceed (the steering row is consumed
#            by the next node's context_assemble),
#   - NO   → write a .steering-checkpoint marker + JSON, pause the run.
# The external driver (dashboard, product backend) sees the awaiting marker,
# collects the human's steering, POSTs it to /api/v1/task-runs/<id>/steer, then
# resumes the run — which re-evaluates the gate and now proceeds.
#
# Public API:
#   mo_steering_has_unconsumed <run_id> [role_target]
#       rc=0 when an unconsumed, unexpired operator_steering row exists for the
#       run (or the global NULL-run queue) addressed to the role (or "any").
#   mo_steering_checkpoint_mark <run_id> <node_id> [reason]
#       Write the awaiting-steering sentinel + .steering-checkpoint.json.
#   mo_steering_checkpoint_clear <run_id>
#       Remove the sentinel + marker (called once steering is present).
#   mo_steering_checkpoint_status <run_id>
#       Emit JSON {awaiting, node_id, sentinel_path}.
#   mo_steering_checkpoint_gate <run_id> <node_id> [role_target]
#       Composite used by the dispatcher: rc=0 proceed (steering present, marker
#       cleared), rc=2 pause (marker written). rc=2 mirrors cost_pause's
#       "halt in place, resume later" contract.

set -uo pipefail

# Reuse the db-path + now-ms helpers from operator_steering.sh.
_MO_STEERING_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
[ -f "$_MO_STEERING_LIB_DIR/operator_steering.sh" ] && . "$_MO_STEERING_LIB_DIR/operator_steering.sh"

_mo_steering_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"steering_checkpoint","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_steering_db() {
  if declare -F _operator_steering_db >/dev/null 2>&1; then
    _operator_steering_db
  else
    echo "${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}"
  fi
}

_mo_steering_now_ms() {
  if declare -F _operator_steering_now_ms >/dev/null 2>&1; then
    _operator_steering_now_ms
  else
    python3 -c 'import time; print(int(time.time() * 1000))' 2>/dev/null || echo $(($(date +%s) * 1000))
  fi
}

_mo_steering_run_dir() {
  local _run_id="$1"
  if [ -n "${MINI_ORK_RUN_DIR:-}" ] && [ -d "$MINI_ORK_RUN_DIR" ]; then
    printf '%s\n' "$MINI_ORK_RUN_DIR"
  else
    printf '%s\n' "${MINI_ORK_HOME:-.mini-ork}/runs/$_run_id"
  fi
}

_mo_steering_sentinel() {
  printf '%s/.steering-checkpoint\n' "$(_mo_steering_run_dir "$1")"
}

_mo_steering_marker() {
  printf '%s/.steering-checkpoint.json\n' "$(_mo_steering_run_dir "$1")"
}

# rc=0 when an actionable steering row exists for this run / role; rc=1 otherwise.
mo_steering_has_unconsumed() {
  local _run_id="${1:-}"
  local _role="${2:-any}"
  [ -n "$_run_id" ] || { _mo_steering_log error "mo_steering_has_unconsumed: run_id required"; return 2; }

  local _db _now
  _db="$(_mo_steering_db)"
  [ -f "$_db" ] || return 1  # no db → nothing to consume
  _now="$(_mo_steering_now_ms)"

  python3 - "$_db" "$_run_id" "$_role" "$_now" <<'PY'
import sqlite3, sys
db, run_id, role, now = sys.argv[1:5]
now = int(now)
con = sqlite3.connect(db, timeout=5.0)
try:
    con.execute("PRAGMA busy_timeout = 5000")
    # A row is actionable when: unconsumed, unexpired, targets this run (or the
    # global NULL-run queue), and addresses this role (or "any" on either side).
    row = con.execute(
        """SELECT 1 FROM operator_steering
            WHERE consumed_at IS NULL
              AND expires_at > ?
              AND (run_id = ? OR run_id IS NULL)
              AND (role_target = ? OR role_target = 'any' OR ? = 'any')
            LIMIT 1""",
        (now, run_id, role, role),
    ).fetchone()
finally:
    con.close()
sys.exit(0 if row else 1)
PY
}

mo_steering_checkpoint_mark() {
  local _run_id="${1:-}" _node_id="${2:-}" _reason="${3:-}"
  [ -n "$_run_id" ] || return 2
  local _dir _sentinel _marker _now
  _dir="$(_mo_steering_run_dir "$_run_id")"
  mkdir -p "$_dir" 2>/dev/null || true
  _sentinel="$(_mo_steering_sentinel "$_run_id")"
  _marker="$(_mo_steering_marker "$_run_id")"
  _now="$(_mo_steering_now_ms)"
  : > "$_sentinel"
  REASON="$_reason" python3 - "$_marker" "$_run_id" "$_node_id" "$_now" <<'PY' 2>/dev/null || true
import json, os, sys
marker, run_id, node_id, now = sys.argv[1:5]
with open(marker, "w") as f:
    json.dump({
        "awaiting_steering": True,
        "run_id": run_id,
        "node_id": node_id,
        "reason": os.environ.get("REASON") or None,
        "requested_at_ms": int(now),
    }, f)
PY
  _mo_steering_log info "checkpoint awaiting steering: run=$_run_id node=$_node_id"
}

mo_steering_checkpoint_clear() {
  local _run_id="${1:-}"
  [ -n "$_run_id" ] || return 2
  rm -f "$(_mo_steering_sentinel "$_run_id")" "$(_mo_steering_marker "$_run_id")" 2>/dev/null || true
}

mo_steering_checkpoint_status() {
  local _run_id="${1:-}"
  [ -n "$_run_id" ] || { echo '{"awaiting":false}'; return 0; }
  local _sentinel _marker
  _sentinel="$(_mo_steering_sentinel "$_run_id")"
  _marker="$(_mo_steering_marker "$_run_id")"
  if [ -f "$_sentinel" ]; then
    if [ -f "$_marker" ]; then
      SENTINEL="$_sentinel" python3 - "$_marker" <<'PY' 2>/dev/null || echo "{\"awaiting\":true,\"sentinel_path\":\"$_sentinel\"}"
import json, os, sys
with open(sys.argv[1]) as f:
    m = json.load(f)
m["awaiting"] = True
m["sentinel_path"] = os.environ.get("SENTINEL")
print(json.dumps(m))
PY
    else
      printf '{"awaiting":true,"sentinel_path":"%s"}\n' "$_sentinel"
    fi
  else
    echo '{"awaiting":false}'
  fi
}

# Composite gate the dispatcher calls when plan_status=needs_steering.
# rc=0 → steering present (marker cleared), proceed.
# rc=2 → no steering yet (marker written), pause the run.
mo_steering_checkpoint_gate() {
  local _run_id="${1:-}" _node_id="${2:-checkpoint}" _role="${3:-any}"
  [ -n "$_run_id" ] || { _mo_steering_log error "gate: run_id required"; return 2; }
  if mo_steering_has_unconsumed "$_run_id" "$_role"; then
    mo_steering_checkpoint_clear "$_run_id"
    _mo_steering_log info "checkpoint satisfied: run=$_run_id node=$_node_id"
    return 0
  fi
  mo_steering_checkpoint_mark "$_run_id" "$_node_id" "awaiting human steering"
  return 2
}
