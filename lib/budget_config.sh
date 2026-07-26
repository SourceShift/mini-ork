# lib/budget_config.sh — single source of truth for the daily cost cap.
#
# Contract (referenced by lib/llm-dispatch.sh:1639 and bin/mini-ork-conductor):
#   1. MO_DAILY_BUDGET_USD env override wins.
#   2. Else config/agents.yaml → budget.daily_cap_usd.
#   3. Else 50 (the historical fallback, identical to the old
#      ${MO_DAILY_BUDGET_USD:-50.0}).
#
# This file was referenced since the cost-circuit work but never committed —
# bin/mini-ork-conductor aborted under `set -e` on MINI_ORK_RUNTIME=bash, and
# llm-dispatch.sh silently fell back to 50 via its declare -F guard.

mo_daily_budget_cap() {
  if [ -n "${MO_DAILY_BUDGET_USD:-}" ]; then
    printf '%s\n' "$MO_DAILY_BUDGET_USD"
    return 0
  fi
  local cfg="${MINI_ORK_ROOT:-.}/config/agents.yaml"
  if [ -f "$cfg" ]; then
    local cap
    cap="$(sed -n 's/^[[:space:]]*daily_cap_usd:[[:space:]]*\([0-9][0-9.]*\).*/\1/p' "$cfg" | head -1)"
    if [ -n "$cap" ]; then
      printf '%s\n' "$cap"
      return 0
    fi
  fi
  printf '50\n'
}
