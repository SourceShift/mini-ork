#!/usr/bin/env bash
# cost_pause.sh — reactive cost UX: pause every N USD spent.
#
# Implements Epic E4 of the Omnigent-improvement plan
# (.mini-ork/kickoffs/omnigent-phase-e4-cost-pause.md) per the
# panel-revised ordering.
#
# Pairs with mini_ork.cost_advisor (Epic E3 — proactive cost control).
# Cost-pause is REACTIVE: kill the dispatch in place when cumulative
# run cost crosses MO_PAUSE_EVERY_USD. The operator runs
# `bin/mini-ork-resume <run_id>` to clear the sentinel and unblock
# the next dispatch step.
#
# Why this matters: today MINI_ORK_DAILY_BUDGET_USD is the only
# guardrail, granular to a whole day. Expensive single dispatches
# (recursive-validate-impl's 5-tier loop, ~$25/iter) need finer
# control — pause per iteration, not per day.
#
# Public API:
#   mo_cost_pause_check <run_id> <delta_usd>
#       When cumulative run cost crosses MO_PAUSE_EVERY_USD (default
#       $25), writes ${MINI_ORK_RUN_DIR}/.cost-pause sentinel +
#       returns rc=2. Otherwise rc=0.
#   mo_cost_pause_status <run_id>
#       Emits JSON {paused, threshold_usd, spent_usd, sentinel_path}.
#
# Env knobs:
#   MO_PAUSE_EVERY_USD   USD threshold per pause window. Default 25.

set -uo pipefail

_mo_cost_pause_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"cost_pause","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_cost_pause_sentinel() {
  local _run_id="$1"
  if [ -n "${MINI_ORK_RUN_DIR:-}" ] && [ -d "$MINI_ORK_RUN_DIR" ]; then
    printf '%s\n' "$MINI_ORK_RUN_DIR/.cost-pause"
  else
    printf '%s\n' "${MINI_ORK_HOME:-.mini-ork}/runs/$_run_id/.cost-pause"
  fi
}

mo_cost_pause_check() {
  local _run_id="${1:-}"
  local _delta="${2:-0}"

  if [ -z "$_run_id" ]; then
    _mo_cost_pause_log "error" "mo_cost_pause_check: run_id required"
    return 2
  fi

  local _threshold="${MO_PAUSE_EVERY_USD:-25}"
  local _sentinel
  _sentinel=$(_mo_cost_pause_sentinel "$_run_id")

  # Track cumulative spend per run in a sidecar file. The file lives
  # next to the sentinel so cleanup is a single rm.
  local _spend_file="${_sentinel%.cost-pause}.cost-spent"
  local _prev_spent=0
  if [ -f "$_spend_file" ]; then
    _prev_spent=$(cat "$_spend_file")
  fi

  # Use python3 for floating-point arithmetic; bash can't.
  local _new_spent
  _new_spent=$(python3 -c "print(float('$_prev_spent') + float('$_delta'))" 2>/dev/null || echo "$_prev_spent")
  printf '%s\n' "$_new_spent" > "$_spend_file"

  # Threshold crossed? Use integer ceiling div to find pause window.
  local _crossed
  _crossed=$(python3 -c "
prev = float('$_prev_spent')
new = float('$_new_spent')
thr = float('$_threshold')
prev_window = int(prev // thr)
new_window  = int(new  // thr)
print('1' if new_window > prev_window else '0')
")

  if [ "$_crossed" = "1" ]; then
    printf '{"threshold_usd":%s,"spent_usd":%s,"created_at":"%s","run_id":"%s"}\n' \
      "$_threshold" "$_new_spent" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_run_id" \
      > "$_sentinel"
    _mo_cost_pause_log "info" "run $_run_id paused at \$$_new_spent; resume with bin/mini-ork-resume $_run_id"
    return 2
  fi
  return 0
}

mo_cost_pause_status() {
  local _run_id="${1:-}"
  if [ -z "$_run_id" ]; then
    _mo_cost_pause_log "error" "mo_cost_pause_status: run_id required"
    return 2
  fi

  local _threshold="${MO_PAUSE_EVERY_USD:-25}"
  local _sentinel
  _sentinel=$(_mo_cost_pause_sentinel "$_run_id")
  local _spend_file="${_sentinel%.cost-pause}.cost-spent"
  local _spent=0
  [ -f "$_spend_file" ] && _spent=$(cat "$_spend_file")
  local _paused=false
  [ -f "$_sentinel" ] && _paused=true

  printf '{"paused":%s,"threshold_usd":%s,"spent_usd":%s,"sentinel_path":"%s"}\n' \
    "$_paused" "$_threshold" "$_spent" "$_sentinel"
}

# Self-test: 3 fixtures (under-threshold no-pause / over-threshold
# writes sentinel / repeat-check on existing sentinel does not crash).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _selftest_dir=$(mktemp -d)
  trap 'rm -rf "$_selftest_dir"' EXIT
  export MINI_ORK_RUN_DIR="$_selftest_dir"
  export MO_PAUSE_EVERY_USD=10

  echo "--- fixture 1: under-threshold spend (expect rc=0, no sentinel) ---"
  if mo_cost_pause_check "test-run" "5.00" 2>/dev/null; then
    if [ ! -f "$MINI_ORK_RUN_DIR/.cost-pause" ]; then
      echo "  [ok] no sentinel after \$5 spent"
    else
      echo "  [fail] sentinel exists too early"
    fi
  else
    echo "  [fail] should have returned rc=0"
  fi

  echo "--- fixture 2: cross-threshold spend (expect rc=2 + sentinel) ---"
  if mo_cost_pause_check "test-run" "8.00" 2>/dev/null; then
    echo "  [fail] should have returned rc=2 after crossing \$10"
  else
    if [ -f "$MINI_ORK_RUN_DIR/.cost-pause" ]; then
      echo "  [ok] sentinel written at \$13 total"
    else
      echo "  [fail] sentinel missing"
    fi
  fi

  echo "--- fixture 3: status query reads back state cleanly ---"
  out=$(mo_cost_pause_status "test-run" 2>/dev/null)
  if echo "$out" | grep -q '"paused":true' && echo "$out" | grep -q 'threshold_usd'; then
    echo "  [ok] status returns paused=true + threshold"
  else
    echo "  [fail] status broken: $out"
  fi
fi
