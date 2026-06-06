#!/usr/bin/env bash
# gates/liveness.sh — recipe-level behavioral liveness gate shim.
#
# Wraps lib/circuit_breaker.sh::mo_check_liveness_breaker. Halts a
# recipe run that is burning cost without producing forward progress —
# the failure mode that v0.2 Phase D's cost-CB (MO_DAILY_BUDGET_USD)
# cannot catch because spend is still under the cap. Behavioral
# complement to the cost-CB.
#
# Three orthogonal stagnation signals (Phase O fail-open pattern):
#   1. artifact_hash invariant across last N task_runs in scope
#   2. last M reviewer_verdicts identical and non-APPROVE
#   3. Σ cost_usd > MO_CB_COST_THRESHOLD with 0 unique files_written
#
# Decision policy MO_CB_POLICY ∈ {majority,or,and}, default majority
# (2-of-3). Single noisy signal cannot trip alone.
#
# Context JSON contract:
#   { "run_id": "<run-id>" }
#   OR (back-compat with the central wire-up in bin/mini-ork-execute):
#   { "panel_run_id": "<run-id>" }
#
# Env knobs (forwarded to the lib):
#   MO_CB_ARTIFACT_WINDOW    default 3
#   MO_CB_VERDICT_WINDOW     default 3
#   MO_CB_COST_THRESHOLD     default 1.00 USD
#   MO_CB_POLICY             default "majority"
#   MO_CB_COOLDOWN_S         default 1800 (30 min, ralph-claude-code parity)
#   MO_CB_DISABLE            set to 1 → always PROCEED (log-only mode)
#
# rc semantic:
#   0 → PROCEED or PROBE (recipe may continue this iteration)
#   1 → LIVENESS_TRIP (recipe MUST halt — burning cost without progress)
#   2 → defer (lib unloadable OR context lacks run_id — fail-open)

set +e
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

context="${1:-}"
if [ -z "$context" ]; then
  printf '{"verdict":"defer","reason":"no context json"}\n'
  exit 2
fi

# Accept either `run_id` (preferred) or `panel_run_id` (the central
# wire-up in bin/mini-ork-execute uses the latter as a shared key across
# all oracle gates). Liveness operates on the top-level task_runs.id, so
# either form maps to the same lookup.
run_id=$(printf '%s' "$context" | jq -r '.run_id // .panel_run_id // empty' 2>/dev/null)

if [ -z "$run_id" ]; then
  printf '{"verdict":"defer","reason":"context missing run_id/panel_run_id"}\n'
  exit 2
fi

# shellcheck source=../lib/circuit_breaker.sh
source "$MINI_ORK_ROOT/lib/circuit_breaker.sh" 2>/dev/null || {
  printf '{"verdict":"defer","reason":"circuit_breaker lib not loadable"}\n'
  exit 2
}

result=$(mo_check_liveness_breaker "$run_id" 2>/dev/null)
verdict=$(printf '%s' "$result" | jq -r '.verdict // "PROCEED"' 2>/dev/null)
printf '%s\n' "$result"

case "$verdict" in
  PROCEED|PROBE) exit 0 ;;
  LIVENESS_TRIP) exit 1 ;;
  *)             exit 2 ;;
esac
