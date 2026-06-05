#!/usr/bin/env bash
# readme-drift-providers-doctor.sh — Pre-flight liveness probe for the
# 4-lens panel providers.
#
# Each lens provider gets a 1-token "say <NAME>_OK only" prompt with a
# tight timeout. If fewer than MO_DRIFT_MIN_RESPONSIVE_LENSES (default
# 2) respond cleanly within MO_DRIFT_PROBE_TIMEOUT_SEC (default 20),
# the doctor returns rc=1 and the panel caller can skip the panel
# entirely with reason=panel_unavailable.
#
# Why this exists: today's smoke (2026-06-05) revealed 4/5 lens
# providers silent-fail or timeout. Running the panel anyway burns
# 4 × 90s = 360s wall on guaranteed-empty calls + an opus arbiter
# call that has nothing to arbitrate. The doctor cuts that to a
# 20s pre-flight that decides whether the panel can produce signal
# AT ALL.
#
# Exit codes:
#   0  panel viable (≥ MO_DRIFT_MIN_RESPONSIVE_LENSES providers responded)
#   1  panel NOT viable (fewer than the threshold responded)
#   2  invocation error (secrets file missing, etc)
#
# Output: JSON object on stdout:
#   { "viable":            true | false,
#     "threshold":         <int>,
#     "responsive_count":  <int>,
#     "providers": {
#       "<name>": { "responsive": <bool>, "wall_sec": <int>, "rc": <int>, "note": "..." }
#     } }

set +e
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SECRETS="${MO_SECRETS:-$MINI_ORK_ROOT/.mini-ork/config/secrets.local.sh}"
[ -f "$SECRETS" ] || SECRETS="$HOME/.config/mini-ork/secrets.local.sh"
[ -f "$SECRETS" ] && source "$SECRETS"

THRESHOLD="${MO_DRIFT_MIN_RESPONSIVE_LENSES:-2}"
PROBE_TIMEOUT="${MO_DRIFT_PROBE_TIMEOUT_SEC:-20}"

# Provider list: name + sourceable|executable.
LENSES=(
  "codex_lens:codex:executable"
  "kimi_lens:kimi:sourceable"
  "minimax_lens:minimax:sourceable"
  "glm_lens:glm:sourceable"
)

probe_one() {
  local lens_name="$1" provider="$2" kind="$3"
  local provider_path="$MINI_ORK_ROOT/lib/providers/cl_${provider}.sh"
  local t1 t2 rc out
  local note=""

  if [ ! -f "$provider_path" ]; then
    echo '{"responsive":false,"wall_sec":0,"rc":-1,"note":"provider script missing"}'
    return
  fi

  t1=$(date +%s)
  if [ "$kind" = "executable" ]; then
    out=$( timeout "$PROBE_TIMEOUT" "$provider_path" --print --output-format text \
             "Say ${lens_name}_OK only." < /dev/null 2>/dev/null )
    rc=$?
  else
    out=$(
      source "$provider_path" 2>/dev/null
      timeout "$PROBE_TIMEOUT" claude --print --output-format text \
        "Say ${lens_name}_OK only." < /dev/null 2>/dev/null
    )
    rc=$?
  fi
  t2=$(date +%s)
  local wall=$((t2 - t1))

  # "Responsive" = rc=0 AND non-empty stdout AND not an obvious error string.
  local responsive=false
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
    if echo "$out" | grep -qiE "(error|failed|exception|no stdin data received)" 2>/dev/null; then
      note="rc=0 but stdout contains error string"
    else
      responsive=true
    fi
  elif [ "$rc" -eq 124 ]; then
    note="timeout after ${PROBE_TIMEOUT}s"
  elif [ "$rc" -eq 0 ] && [ -z "$out" ]; then
    note="rc=0 but empty stdout (silent fail)"
  else
    note="rc=$rc"
  fi

  printf '{"responsive":%s,"wall_sec":%d,"rc":%d,"note":%s}\n' \
    "$responsive" "$wall" "$rc" "$(printf '%s' "$note" | jq -Rs .)"
}

declare -a results=()
responsive_count=0
for spec in "${LENSES[@]}"; do
  IFS=':' read -r lens_name provider kind <<< "$spec"
  result=$(probe_one "$lens_name" "$provider" "$kind")
  results+=("\"${lens_name}\":${result}")
  if echo "$result" | jq -e '.responsive' >/dev/null 2>&1; then
    responsive_count=$((responsive_count + 1))
  fi
done

viable=false
[ "$responsive_count" -ge "$THRESHOLD" ] && viable=true

printf '{"viable":%s,"threshold":%d,"responsive_count":%d,"providers":{%s}}\n' \
  "$viable" "$THRESHOLD" "$responsive_count" "$(IFS=,; echo "${results[*]}")"

[ "$viable" = "true" ] && exit 0 || exit 1
