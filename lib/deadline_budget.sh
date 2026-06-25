#!/usr/bin/env bash
# deadline_budget.sh — per-request wall-clock --deadline <seconds> budget.
#
# Sibling of lib/cost_pause.sh. That file owns the dollar-cap pause
# sentinel; this file owns the wall-clock deadline sentinel. Both write
# sidecars under MINI_ORK_RUN_DIR but stay independent — neither mutates
# the other's data — so the dollar-cap pause behavior in lib/cost_pause.sh
# remains intact while the deadline budget is wired through bin/mini-ork.
#
# Pairs with the run-loop's between-stage gates: the loop calls
# mo_deadline_check after each stage and skips remaining work on hit.
# A long stage can overshoot the requested wall-clock — by design — and
# the overshoot is captured in the deadline_hit sentinel payload.
#
# Public API:
#   mo_deadline_init <run_id> <seconds> [<run_dir>]
#       Arms a deadline. Sets MO_DEADLINE_EPOCH / MO_DEADLINE_SECONDS /
#       MO_DEADLINE_START env vars and writes ${run_dir}/.deadline-budget
#       sidecar JSON. Idempotent on repeat invocation (re-arms).
#   mo_deadline_check <run_id>
#       Returns 0 while budget remains, rc=2 once exhausted. The FIRST
#       rc=2 also writes ${run_dir}/.deadline-hit sentinel JSON and emits
#       a `deadline_hit` JSON log line on stderr. Subsequent calls keep
#       returning rc=2 (sentinel acts as latched flag) so between-stage
#       gates stay short-circuit-safe.
#   mo_deadline_status <run_id>
#       Emits JSON {run_id, deadline_seconds, start_epoch, deadline_epoch,
#       elapsed_seconds, remaining_seconds, hit, sidecar_path,
#       sentinel_path}.
#
# Env knobs:
#   MO_DEADLINE_EPOCH     Explicit deadline epoch (epoch seconds). Used
#                         directly by mo_deadline_check when set; takes
#                         precedence over a stale sidecar.
#   MO_DEADLINE_SECONDS   Seconds budget; recorded in sentinel/status
#                         payloads for observability.
#   MO_DEADLINE_START     When the budget was armed; lets mo_deadline_check
#                         compute elapsed without re-reading the sidecar.

set -uo pipefail

_mo_deadline_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"deadline_budget","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_deadline_run_dir() {
  local _run_id="$1"
  if [ -n "${MINI_ORK_RUN_DIR:-}" ] && [ -d "$MINI_ORK_RUN_DIR" ]; then
    printf '%s\n' "$MINI_ORK_RUN_DIR"
  else
    printf '%s\n' "${MINI_ORK_HOME:-.mini-ork}/runs/$_run_id"
  fi
}

_mo_deadline_sidecar() {
  local _run_dir="$1"
  printf '%s\n' "$_run_dir/.deadline-budget"
}

_mo_deadline_hit_sentinel() {
  local _run_dir="$1"
  printf '%s\n' "$_run_dir/.deadline-hit"
}

mo_deadline_init() {
  local _run_id="${1:-}"
  local _seconds="${2:-}"
  local _run_dir="${3:-}"

  if [ -z "$_run_id" ] || [ -z "$_seconds" ]; then
    _mo_deadline_log "error" "mo_deadline_init: run_id and seconds required"
    return 2
  fi

  case "$_seconds" in
    ''|*[!0-9]*)
      _mo_deadline_log "error" "mo_deadline_init: seconds must be a positive integer (got '$_seconds')"
      return 2 ;;
  esac
  if [ "$_seconds" -le 0 ]; then
    _mo_deadline_log "error" "mo_deadline_init: seconds must be > 0 (got $_seconds)"
    return 2
  fi

  if [ -z "$_run_dir" ]; then
    _run_dir=$(_mo_deadline_run_dir "$_run_id")
  fi
  [ -d "$_run_dir" ] || mkdir -p "$_run_dir"

  local _start _deadline _sidecar
  _start=$(date +%s)
  _deadline=$(( _start + _seconds ))
  export MO_DEADLINE_EPOCH="$_deadline"
  export MO_DEADLINE_SECONDS="$_seconds"
  export MO_DEADLINE_START="$_start"

  _sidecar=$(_mo_deadline_sidecar "$_run_dir")
  printf '{"run_id":"%s","deadline_seconds":%s,"start_epoch":%s,"deadline_epoch":%s,"created_at":"%s"}\n' \
    "$_run_id" "$_seconds" "$_start" "$_deadline" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$_sidecar"

  _mo_deadline_log "info" "run $_run_id deadline armed: ${_seconds}s (epoch $_deadline)"
  return 0
}

mo_deadline_check() {
  local _run_id="${1:-}"
  if [ -z "$_run_id" ]; then
    _mo_deadline_log "error" "mo_deadline_check: run_id required"
    return 2
  fi
  if [ -z "${MO_DEADLINE_EPOCH:-}" ]; then
    # No deadline armed — open budget. Lets other tools source this lib
    # without forcing a budget.
    return 0
  fi

  local _run_dir _hit_sentinel
  _run_dir=$(_mo_deadline_run_dir "$_run_id")
  _hit_sentinel=$(_mo_deadline_hit_sentinel "$_run_dir")
  # Latched: once tripped, stay tripped. Avoids re-emitting markers and
  # keeps rc=2 stable across repeat between-stage checks.
  if [ -f "$_hit_sentinel" ]; then
    return 2
  fi

  local _now _remaining _elapsed _start _best_artifact
  _now=$(date +%s)
  _remaining=$(( MO_DEADLINE_EPOCH - _now ))
  _start="${MO_DEADLINE_START:-$_now}"
  _elapsed=$(( _now - _start ))
  if [ "$_remaining" -gt 0 ]; then
    return 0
  fi

  # Deadline hit. Record best-so-far artifact when available — this is
  # the contract the run loop relies on for clean partial-completion exit.
  _best_artifact=""
  if [ -n "${MINI_ORK_ARTIFACT_PATH:-}" ] && [ -f "${MINI_ORK_ARTIFACT_PATH}" ]; then
    _best_artifact="$MINI_ORK_ARTIFACT_PATH"
  fi

  printf '{"run_id":"%s","deadline_seconds":%s,"start_epoch":%s,"deadline_epoch":%s,"hit_at":"%s","elapsed_seconds":%s,"remaining_seconds":%s,"best_so_far_artifact":"%s","finish_reason":"deadline_hit","note":"soft-stop between stages; one stage may overshoot requested wall-clock"}\n' \
    "$_run_id" "${MO_DEADLINE_SECONDS:-0}" "$_start" "$MO_DEADLINE_EPOCH" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_elapsed" "$_remaining" "$_best_artifact" \
    > "$_hit_sentinel"

  # Marker line for downstream grep (verifier, ops, traces). Mirrors the
  # cost_pause convention: one JSON line per event on stderr.
  printf '{"event_type":"deadline_hit","subsystem":"deadline_budget","run_id":"%s","elapsed_seconds":%s,"remaining_seconds":%s,"best_so_far_artifact":"%s","finish_reason":"deadline_hit"}\n' \
    "$_run_id" "$_elapsed" "$_remaining" "$_best_artifact" >&2
  _mo_deadline_log "info" "run $_run_id deadline_hit after ${_elapsed}s; best-so-far=${_best_artifact:-none}"
  return 2
}

mo_deadline_status() {
  local _run_id="${1:-}"
  if [ -z "$_run_id" ]; then
    _mo_deadline_log "error" "mo_deadline_status: run_id required"
    return 2
  fi
  local _run_dir _sidecar _hit_sentinel
  _run_dir=$(_mo_deadline_run_dir "$_run_id")
  _sidecar=$(_mo_deadline_sidecar "$_run_dir")
  _hit_sentinel=$(_mo_deadline_hit_sentinel "$_run_dir")

  local _hit=false
  [ -f "$_hit_sentinel" ] && _hit=true
  local _start="${MO_DEADLINE_START:-0}"
  local _deadline="${MO_DEADLINE_EPOCH:-0}"
  local _seconds="${MO_DEADLINE_SECONDS:-0}"
  local _now _elapsed=0 _remaining=0
  _now=$(date +%s)
  if [ "$_start" -gt 0 ]; then
    _elapsed=$(( _now - _start ))
  fi
  if [ "$_deadline" -gt 0 ]; then
    _remaining=$(( _deadline - _now ))
  fi
  printf '{"run_id":"%s","deadline_seconds":%s,"start_epoch":%s,"deadline_epoch":%s,"elapsed_seconds":%s,"remaining_seconds":%s,"hit":%s,"sidecar_path":"%s","sentinel_path":"%s"}\n' \
    "$_run_id" "$_seconds" "$_start" "$_deadline" "$_elapsed" "$_remaining" \
    "$_hit" "$_sidecar" "$_hit_sentinel"
}

# Self-test: arm budget, verify in-budget check, then exhaust and verify
# sentinel + finish_reason. Same gating as lib/cost_pause.sh: only runs
# when this script is invoked directly (not sourced).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT
  export MINI_ORK_RUN_DIR="$_selftest_dir"
  _dl_run_id="dl-selftest"

  echo "--- fixture 1: init arms budget and writes sidecar ---"
  if mo_deadline_init "$_dl_run_id" 5 "$_selftest_dir" 2>/dev/null; then
    if [ -f "$_selftest_dir/.deadline-budget" ] \
       && grep -q '"deadline_seconds":5' "$_selftest_dir/.deadline-budget"; then
      echo "  [ok] sidecar written with deadline_seconds=5"
    else
      echo "  [fail] sidecar missing or malformed"
    fi
  else
    echo "  [fail] init returned non-zero"
  fi

  echo "--- fixture 2: check while budget remains returns rc=0 ---"
  if mo_deadline_check "$_dl_run_id" 2>/dev/null; then
    if [ ! -f "$_selftest_dir/.deadline-hit" ]; then
      echo "  [ok] check passes while budget remains"
    else
      echo "  [fail] hit sentinel present too early"
    fi
  else
    echo "  [fail] check should have returned rc=0"
  fi

  echo "--- fixture 3: re-init 1s + sleep → check trips rc=2 + sentinel ---"
  mo_deadline_init "$_dl_run_id" 1 "$_selftest_dir" 2>/dev/null
  sleep 2
  if mo_deadline_check "$_dl_run_id" 2>/dev/null; then
    echo "  [fail] check should have returned rc=2 after deadline"
  else
    if [ -f "$_selftest_dir/.deadline-hit" ] \
       && grep -q '"finish_reason":"deadline_hit"' "$_selftest_dir/.deadline-hit"; then
      echo "  [ok] hit sentinel written with finish_reason=deadline_hit"
    else
      echo "  [fail] hit sentinel missing or malformed"
    fi
  fi

  echo "--- fixture 4: status reports hit=true ---"
  out=$(mo_deadline_status "$_dl_run_id" 2>/dev/null)
  if echo "$out" | grep -q '"hit":true'; then
    echo "  [ok] status reports hit=true"
  else
    echo "  [fail] status broken: $out"
  fi
fi