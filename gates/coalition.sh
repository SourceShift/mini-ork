#!/usr/bin/env bash
# gates/coalition.sh — recipe-level coalition gate shim.
#
# Wraps lib/coalition_gate.sh::mo_check_panel_coalition for invocation
# via gate_registry. Recipes opt in by registering this script as a
# `custom` gate type pointing at this path. Example:
#
#   source lib/gate_registry.sh
#   gate_register "custom" "$MINI_ORK_ROOT/gates/coalition.sh" "refactor_audit"
#
# gate_evaluate then exec's this script with the context JSON on argv[1]
# and reads stdout / exit code:
#   rc=0 → pass (panel diverse OR fewer than 2 lens traces)
#   rc=1 → fail (coalition_abort — synthesizer should refuse to run)
#   rc=2 → defer (topology lib unavailable, fail-open advisory)
#
# Context JSON contract:
#   { "panel_run_id": "<run-id>", "recipe": "<recipe-name>" }
#
# Env knobs (forwarded to the lib):
#   MO_RHO_THRESHOLD          default 0.25 (Rajan 2025 ceiling)
#   MO_FAMILY_DIVERSITY_GATE  "strict" | "advisory" (default strict)

set +e
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

context="${1:-}"
if [ -z "$context" ]; then
  printf '{"verdict":"defer","reason":"no context json provided"}\n'
  exit 2
fi

panel_run_id=$(printf '%s' "$context" | jq -r '.panel_run_id // empty' 2>/dev/null)
recipe=$(printf '%s' "$context" | jq -r '.recipe // empty' 2>/dev/null)

if [ -z "$panel_run_id" ] || [ -z "$recipe" ]; then
  printf '{"verdict":"defer","reason":"context missing panel_run_id or recipe"}\n'
  exit 2
fi

# shellcheck source=../lib/coalition_gate.sh
source "$MINI_ORK_ROOT/lib/coalition_gate.sh" 2>/dev/null || {
  printf '{"verdict":"defer","reason":"coalition_gate lib not loadable"}\n'
  exit 2
}

result=$(mo_check_panel_coalition "$panel_run_id" "$recipe" 2>/dev/null)
verdict=$(printf '%s' "$result" | jq -r '.verdict // "indeterminate"' 2>/dev/null)
printf '%s\n' "$result"

case "$verdict" in
  panel_diverse)    exit 0 ;;
  COALITION_ABORT)  exit 1 ;;
  indeterminate)    exit 2 ;;
  *)                exit 2 ;;
esac
