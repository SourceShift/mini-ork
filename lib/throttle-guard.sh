#!/usr/bin/env bash
# throttle-guard.sh — provider-throttle classification + per-lane backoff
# state for the recursive-self-improve outer loop (and any other long-
# running mini-ork driver that wants to sleep through transient
# capacity / rate-limit / overload errors instead of spinning empty
# iters into a black hole.
#
# Public API:
#   _throttle_classify_error      <err_log_path> → "throttled|auth_failed|timed_out|capacity|overloaded|unknown"
#   _throttle_record_failure      <provider> <classification>
#   _throttle_check_cooldown      <provider> → echoes seconds-until-resume (0 if ready)
#   _throttle_clear_on_success    <provider>
#   _throttle_systemic_halt_check → returns 0 (true: halt) if N+ providers are simultaneously throttled
#
# State file format ($MINI_ORK_HOME/state/throttle-<provider>.flag):
#   cool_down_until=<epoch>
#   consecutive_failures=<int>
#   last_error=<classification>
#   last_seen=<epoch>

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MINI_ORK_HOME="${MINI_ORK_HOME:-$(pwd)/.mini-ork}"
_THROTTLE_STATE_DIR="$MINI_ORK_HOME/state"

# Backoff ladder in seconds: 5m → 10m → 30m → 1h cap.
_THROTTLE_BACKOFFS=(0 300 600 1800 3600 3600 3600)

# Systemic-halt threshold: N+ distinct providers throttled within the
# observation window means the upstream is having a bad day; halt the
# entire loop until human review.
_THROTTLE_SYSTEMIC_THRESHOLD="${MINI_ORK_THROTTLE_SYSTEMIC_THRESHOLD:-3}"
_THROTTLE_SYSTEMIC_WINDOW_S="${MINI_ORK_THROTTLE_SYSTEMIC_WINDOW_S:-600}"

# Empty-iter safeguard: N consecutive iters with zero lens output means
# the loop is spinning, halt regardless of throttle classifications.
_THROTTLE_EMPTY_ITER_THRESHOLD="${MINI_ORK_THROTTLE_EMPTY_ITER_THRESHOLD:-5}"

_throttle_classify_error() {
  local err_log="${1:?err_log required}"
  [ -f "$err_log" ] || { echo "unknown"; return 0; }

  # OpenAI Codex — server-side capacity (gpt-5.5 globally throttled or
  # account hitting per-window rate limit; CLI doesn't distinguish)
  if grep -qE "Selected model is at capacity|model_overloaded|engine is overloaded" "$err_log" 2>/dev/null; then
    echo "capacity"; return 0
  fi
  # OpenAI / generic 429 rate limit
  if grep -qE "429 Too Many Requests|rate_limit_exceeded|rate limit reached|insufficient_quota" "$err_log" 2>/dev/null; then
    echo "throttled"; return 0
  fi
  # Anthropic overload (Opus / Sonnet)
  if grep -qE "529 |overloaded_error|Service Unavailable" "$err_log" 2>/dev/null; then
    echo "overloaded"; return 0
  fi
  # Auth — NOT a backoff candidate; halt the lane immediately
  if grep -qE "401|authentication_error|invalid_api_key|API Key appears to be invalid" "$err_log" 2>/dev/null; then
    echo "auth_failed"; return 0
  fi
  # Timeout — distinct from throttle (latency, not capacity)
  if grep -qE "gtimeout|timed out|deadline_exceeded" "$err_log" 2>/dev/null; then
    echo "timed_out"; return 0
  fi
  echo "unknown"
}

_throttle_flag_path() {
  local provider="${1:?provider required}"
  printf '%s/throttle-%s.flag\n' "$_THROTTLE_STATE_DIR" "$provider"
}

_throttle_record_failure() {
  local provider="${1:?provider required}"
  local classification="${2:?classification required}"
  mkdir -p "$_THROTTLE_STATE_DIR"
  local flag; flag=$(_throttle_flag_path "$provider")
  local now; now=$(date +%s)

  local prior_failures=0
  if [ -f "$flag" ]; then
    prior_failures=$(awk -F= '/^consecutive_failures=/{print $2}' "$flag" 2>/dev/null || echo 0)
  fi
  prior_failures=$((prior_failures + 1))

  # Auth failures DON'T enter the backoff ladder — they need human action.
  # Record so the orchestrator can see, but cool_down_until=0.
  local cool_seconds=0
  case "$classification" in
    capacity|throttled|overloaded|timed_out)
      local idx="$prior_failures"
      [ "$idx" -ge "${#_THROTTLE_BACKOFFS[@]}" ] && idx=$((${#_THROTTLE_BACKOFFS[@]} - 1))
      cool_seconds="${_THROTTLE_BACKOFFS[$idx]}"
      ;;
    auth_failed)
      cool_seconds=0   # don't auto-retry; lane is structurally broken
      ;;
    *)
      cool_seconds=60  # unknown errors: short sleep then try
      ;;
  esac

  local cool_until=$((now + cool_seconds))
  {
    echo "cool_down_until=$cool_until"
    echo "consecutive_failures=$prior_failures"
    echo "last_error=$classification"
    echo "last_seen=$now"
  } > "$flag"

  echo "  [throttle] $provider classified=$classification consecutive=$prior_failures cool_down_seconds=$cool_seconds" >&2
}

_throttle_check_cooldown() {
  local provider="${1:?provider required}"
  local flag; flag=$(_throttle_flag_path "$provider")
  [ -f "$flag" ] || { echo 0; return 0; }
  local cool_until; cool_until=$(awk -F= '/^cool_down_until=/{print $2}' "$flag" 2>/dev/null || echo 0)
  local now; now=$(date +%s)
  if [ "$cool_until" -gt "$now" ]; then
    echo $((cool_until - now))
  else
    echo 0
  fi
}

_throttle_clear_on_success() {
  local provider="${1:?provider required}"
  local flag; flag=$(_throttle_flag_path "$provider")
  rm -f "$flag" 2>/dev/null
}

_throttle_systemic_halt_check() {
  # Returns 0 (true: halt) if >= _THROTTLE_SYSTEMIC_THRESHOLD distinct
  # providers have a live cool_down_until > now AND were last seen
  # within _THROTTLE_SYSTEMIC_WINDOW_S of each other.
  [ -d "$_THROTTLE_STATE_DIR" ] || return 1
  local now; now=$(date +%s)
  local count=0
  for flag in "$_THROTTLE_STATE_DIR"/throttle-*.flag; do
    [ -f "$flag" ] || continue
    local cool_until last_seen
    cool_until=$(awk -F= '/^cool_down_until=/{print $2}' "$flag" 2>/dev/null || echo 0)
    last_seen=$(awk -F= '/^last_seen=/{print $2}' "$flag" 2>/dev/null || echo 0)
    if [ "$cool_until" -gt "$now" ] && [ "$((now - last_seen))" -lt "$_THROTTLE_SYSTEMIC_WINDOW_S" ]; then
      count=$((count + 1))
    fi
  done
  [ "$count" -ge "$_THROTTLE_SYSTEMIC_THRESHOLD" ]
}

# Scan a run dir's llm-failures/ for provider error patterns and update
# the per-lane flags. Called by the outer loop after each iter.
_throttle_classify_run_failures() {
  local run_dir="${1:?run_dir required}"
  local failures_dir="$run_dir/llm-failures"
  [ -d "$failures_dir" ] || return 0
  for err_log in "$failures_dir"/*.err.log; do
    [ -f "$err_log" ] || continue
    # File name pattern: <ts>-<provider>.err.log
    local provider
    provider=$(basename "$err_log" .err.log | sed -E 's/^[0-9]+-//')
    [ -n "$provider" ] || continue
    local cls; cls=$(_throttle_classify_error "$err_log")
    [ "$cls" = "unknown" ] && continue
    _throttle_record_failure "$provider" "$cls"
  done
}

# For each provider in a list, sleep until its cool-down expires (or
# return immediately if none of them are throttled). Returns 0 if all
# providers cleared their cool-down; 1 if MINI_ORK_THROTTLE_MAX_SLEEP_S
# was hit without clearing (caller may halt).
_throttle_wait_for_cooldowns() {
  local max_sleep="${MINI_ORK_THROTTLE_MAX_SLEEP_S:-1800}"
  local hard_deadline="${1:-0}"
  shift || true
  local providers=("$@")

  local longest=0
  for p in "${providers[@]}"; do
    local s; s=$(_throttle_check_cooldown "$p")
    [ "$s" -gt "$longest" ] && longest="$s"
  done

  [ "$longest" -eq 0 ] && return 0

  # Cap to remaining hard-deadline budget if provided
  if [ "$hard_deadline" -gt 0 ]; then
    local now; now=$(date +%s)
    local budget=$((hard_deadline - now))
    [ "$budget" -lt "$longest" ] && longest="$budget"
  fi
  [ "$longest" -gt "$max_sleep" ] && longest="$max_sleep"
  [ "$longest" -le 0 ] && return 1

  echo "  [throttle] sleeping ${longest}s for provider cool-down to expire" >&2
  sleep "$longest"
  return 0
}
