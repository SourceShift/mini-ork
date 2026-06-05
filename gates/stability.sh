#!/usr/bin/env bash
# gates/stability.sh — recipe-level adaptive-stability gate shim.
#
# Wraps lib/adaptive_stability.sh::mo_check_panel_stability. Used by
# multi-round panel debates to decide HALT vs CONTINUE between rounds —
# stops debate when verdict drift drops below threshold, saving compute.
#
# Context JSON contract:
#   { "panel_run_id": "<run-id>", "current_round": <int> }
#
# Env knobs (forwarded to the lib):
#   MO_PANEL_STABILITY_THRESHOLD  default 0.10
#   MO_PANEL_MIN_ROUNDS           default 2
#   MO_PANEL_MAX_ROUNDS           default 5
#
# rc semantic (note: stability is a decision aid, NOT a hard gate):
#   0 → CONTINUE (panel still moving — recipe should run another round)
#   1 → HALT (panel stabilized OR max rounds — recipe should stop)
#   2 → defer (lib unloadable)
#
# Recipes that use this gate typically read stdout JSON `.recommendation`
# directly rather than relying on the rc alone.

set +e
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

context="${1:-}"
if [ -z "$context" ]; then
  printf '{"verdict":"defer","reason":"no context json"}\n'
  exit 2
fi

panel_run_id=$(printf '%s' "$context" | jq -r '.panel_run_id // empty' 2>/dev/null)
current_round=$(printf '%s' "$context" | jq -r '.current_round // 1' 2>/dev/null)

if [ -z "$panel_run_id" ]; then
  printf '{"verdict":"defer","reason":"context missing panel_run_id"}\n'
  exit 2
fi

# shellcheck source=../lib/adaptive_stability.sh
source "$MINI_ORK_ROOT/lib/adaptive_stability.sh" 2>/dev/null || {
  printf '{"verdict":"defer","reason":"adaptive_stability lib not loadable"}\n'
  exit 2
}

result=$(mo_check_panel_stability "$panel_run_id" "$current_round" 2>/dev/null)
recommendation=$(printf '%s' "$result" | jq -r '.recommendation // "CONTINUE"' 2>/dev/null)
printf '%s\n' "$result"

case "$recommendation" in
  CONTINUE)  exit 0 ;;
  HALT)      exit 1 ;;
  *)         exit 2 ;;
esac
