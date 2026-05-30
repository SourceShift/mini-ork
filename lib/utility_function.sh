#!/usr/bin/env bash
# utility_function.sh — UtilityScore computation (overridable per task class).
#
# Public API:
#   utility_score <run_result_json> → emits float on stdout
#
# DEFAULT formula (Ch 18):
#   U = 0.45*success + 0.20*verifier_score + 0.15*quality_score
#       - 0.10*norm_cost - 0.05*norm_latency - 0.05*risk_penalty
#
# Override: place a file at
#   ${MINI_ORK_HOME}/config/utility_functions/<task_class>.sh
# that defines utility_score_override(run_result_json).
# The override is sourced and called when task_class matches.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Default weight constants (sum of positive weights = 1.0 when ignoring penalties).
_UTILITY_W_SUCCESS="${MINI_ORK_W_SUCCESS:-0.45}"
_UTILITY_W_VERIFIER="${MINI_ORK_W_VERIFIER:-0.20}"
_UTILITY_W_QUALITY="${MINI_ORK_W_QUALITY:-0.15}"
_UTILITY_W_COST="${MINI_ORK_W_COST:-0.10}"
_UTILITY_W_LATENCY="${MINI_ORK_W_LATENCY:-0.05}"
_UTILITY_W_RISK="${MINI_ORK_W_RISK:-0.05}"

# desc: Compute the utility score for a completed run. run_result_json fields:
#   success (bool|0/1), verifier_score (0-1), quality_score (0-1),
#   cost_usd (float), max_cost_usd (float, normalization ceiling),
#   duration_ms (int), max_duration_ms (int, normalization ceiling),
#   risk_penalty (0-1), task_class (string, triggers per-class override).
# Emits a float in [0.0, 1.0] on stdout.
utility_score() {
  local run_result="${1:?run_result_json required}"

  # Extract task_class to check for per-class override
  local task_class
  task_class="$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('task_class',''))
except Exception:
    print('')
" "$run_result" 2>/dev/null || echo "")"

  # Look for per-class override script
  if [[ -n "$task_class" ]]; then
    local override_dir="${MINI_ORK_HOME:-.mini-ork}/config/utility_functions"
    local override_script="${override_dir}/${task_class}.sh"
    if [[ -f "$override_script" ]]; then
      # shellcheck disable=SC1090
      source "$override_script" 2>/dev/null || true
      if declare -f utility_score_override > /dev/null 2>&1; then
        utility_score_override "$run_result"
        return $?
      fi
    fi
  fi

  # Default formula
  python3 - "$run_result" \
    "$_UTILITY_W_SUCCESS" "$_UTILITY_W_VERIFIER" "$_UTILITY_W_QUALITY" \
    "$_UTILITY_W_COST"    "$_UTILITY_W_LATENCY"  "$_UTILITY_W_RISK" <<'PY'
import json, sys

try:
    r = json.loads(sys.argv[1])
except json.JSONDecodeError as e:
    print(f"utility_score: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

w_success  = float(sys.argv[2])
w_verifier = float(sys.argv[3])
w_quality  = float(sys.argv[4])
w_cost     = float(sys.argv[5])
w_latency  = float(sys.argv[6])
w_risk     = float(sys.argv[7])

def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(v)))

success       = clamp(1.0 if r.get("success") in (True, 1, "true", "1") else 0.0)
verifier_score = clamp(r.get("verifier_score", 0.0))
quality_score  = clamp(r.get("quality_score", 0.5))
risk_penalty   = clamp(r.get("risk_penalty", 0.0))

# Normalize cost and latency — if ceiling provided use it; else treat as 0 penalty.
cost_usd      = float(r.get("cost_usd", 0.0))
max_cost      = float(r.get("max_cost_usd", cost_usd if cost_usd > 0 else 1.0))
norm_cost     = clamp(cost_usd / max_cost) if max_cost > 0 else 0.0

dur_ms        = float(r.get("duration_ms", 0.0))
max_dur       = float(r.get("max_duration_ms", dur_ms if dur_ms > 0 else 1.0))
norm_latency  = clamp(dur_ms / max_dur) if max_dur > 0 else 0.0

U = (w_success  * success
   + w_verifier * verifier_score
   + w_quality  * quality_score
   - w_cost     * norm_cost
   - w_latency  * norm_latency
   - w_risk     * risk_penalty)

# Clamp to [0, 1]
U = max(0.0, min(1.0, U))
print(f"{U:.6f}")
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "utility_function.sh — source me and call utility_score <run_result_json>"
fi
