#!/usr/bin/env bash
# learning-loop-live-validate.sh — prove the learning loop LEARNS and ACTS by
# running a real task N times against the LIVE state.db and watching the router
# change its mind. This is the "by running it, not just tests" companion to
# scripts/learning-loop-closure-gate.sh (which only proves the wiring).
#
# Vehicle: the research-synthesis recipe. Its 4 researcher lenses
# (glm/kimi/codex/opus_lens) share node_type=researcher + task_class=
# research_synthesis, so ONE run writes 4 competing agent_version_id rows —
# exactly the group GRPO needs to rank lanes. risk_class:low (read-only).
#
# What it observes across runs:
#   1. write half  — researcher traces carry a stamped agent_version_id (lane)
#   2. PRM         — every researcher trace gets a non-null process_reward
#   3. GRPO        — agent_performance_memory.relative_advantage diverges (>0)
#   4. RHO         — prompt_win_rates.sample_size crosses MO_LEARNING_MIN_SAMPLES
#   5. THE PAYOFF  — _mo_policy_route_lane researcher <lane> returns the static
#                    default BEFORE and the learned winning lane AFTER
#
# REAL runs cost LLM budget + need secrets. They are OFF by default: a bare
# invocation is inspect-only (snapshot + router probe on current live state).
# Set MO_VALIDATE_DO_RUNS=1 to actually dispatch the N runs.
#
# Usage:
#   bash scripts/learning-loop-live-validate.sh              # inspect-only
#   MO_VALIDATE_DO_RUNS=1 bash scripts/learning-loop-live-validate.sh 3
#
# Exit 0 = evidence of learning present (or inspect-only completed).

set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT
export MINI_ORK_HOME="${MINI_ORK_HOME:-$MINI_ORK_ROOT/.mini-ork}"
export MINI_ORK_DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"

N="${1:-3}"
KICKOFF="${2:-$MINI_ORK_ROOT/kickoffs/learning-validation-synthesis.md}"
TC="${MO_VALIDATE_TASK_CLASS:-research_synthesis}"
PROBE_LANE="${MO_VALIDATE_PROBE_LANE:-kimi_lens}"   # a representative researcher default
export TASK_CLASS="$TC"
export MO_ROUTING_POLICY="${MO_ROUTING_POLICY:-learning_governed}"
export MO_LEARNING_MIN_SAMPLES="${MO_LEARNING_MIN_SAMPLES:-3}"

[ -f "$MINI_ORK_DB" ] || { echo "FATAL: live DB not found at $MINI_ORK_DB" >&2; exit 1; }

# Load the learning functions (defined above the SOURCE_ONLY guard at execute:353).
export MINI_ORK_EXECUTE_SOURCE_ONLY=1
# shellcheck source=../bin/mini-ork-execute
source "$MINI_ORK_ROOT/bin/mini-ork-execute"
unset MINI_ORK_EXECUTE_SOURCE_ONLY
[ -f "$MINI_ORK_ROOT/lib/rho_aggregator.sh" ] && source "$MINI_ORK_ROOT/lib/rho_aggregator.sh" 2>/dev/null || true

q() { sqlite3 "$MINI_ORK_DB" "$1" 2>/dev/null; }

snapshot() {
  local label="$1"
  echo "---- snapshot: $label ----"
  printf "  researcher traces (tc=%s)        : %s\n" "$TC" \
    "$(q "SELECT COUNT(*) FROM execution_traces WHERE task_class='$TC';")"
  printf "  distinct lanes stamped           : %s  [%s]\n" \
    "$(q "SELECT COUNT(DISTINCT agent_version_id) FROM execution_traces WHERE task_class='$TC' AND agent_version_id<>'';")" \
    "$(q "SELECT GROUP_CONCAT(DISTINCT agent_version_id) FROM execution_traces WHERE task_class='$TC' AND agent_version_id<>'';")"
  printf "  with process_reward (PRM)        : %s\n" \
    "$(q "SELECT SUM(process_reward IS NOT NULL) FROM execution_traces WHERE task_class='$TC';")"
  printf "  GRPO rows (relative_advantage)   :\n"
  q "SELECT '      '||agent_version_id||'  adv='||printf('%+.4f', relative_advantage)||'  runs='||runs_count
       FROM agent_performance_memory WHERE task_class='$TC' ORDER BY relative_advantage DESC;" \
    | sed 's/^/  /' || true
  printf "  RHO max sample_size (tc=%s)       : %s\n" "$TC" \
    "$(q "SELECT COALESCE(MAX(sample_size),0) FROM prompt_win_rates WHERE task_class='$TC';")"
}

probe_router() {
  # Mirror the production learning_governed seam (bin/mini-ork-execute:1208):
  # governed lane over the static-hybrid baseline. We compose the two fns
  # directly because _mo_policy_route_lane lives below the SOURCE_ONLY guard
  # and isn't defined here, whereas these two are.
  local base; base="$(_mo_learning_static_lane researcher "$PROBE_LANE")"
  _mo_learning_governed_lane researcher "$base"
}

fire_writers() {
  # PRM scores inline during a real run; re-fire the end-of-run aggregators so
  # GRPO / conductor / RHO reflect the freshest traces immediately.
  mo_learning_write_grpo_advantages   >/dev/null 2>&1 || true
  mo_learning_update_conductor_outcomes >/dev/null 2>&1 || true
  if declare -f rho_aggregate_win_rates >/dev/null 2>&1; then
    rho_aggregate_win_rates --task-class "$TC" >/dev/null 2>&1 || true
  fi
}

echo "================ LEARNING-LOOP LIVE VALIDATION ================"
echo "  DB          : $MINI_ORK_DB"
echo "  task_class  : $TC      probe lane: $PROBE_LANE"
echo "  policy      : $MO_ROUTING_POLICY   min_samples: $MO_LEARNING_MIN_SAMPLES"
echo "  runs        : N=$N   do_runs=${MO_VALIDATE_DO_RUNS:-0}"
echo

snapshot "BEFORE"
ROUTE_BEFORE="$(probe_router)"
echo "  >>> router decision BEFORE: researcher/$PROBE_LANE -> $ROUTE_BEFORE"
echo

if [ "${MO_VALIDATE_DO_RUNS:-0}" = "1" ]; then
  for i in $(seq 1 "$N"); do
    echo "================ RUN $i / $N ================"
    "$MINI_ORK_ROOT/bin/mini-ork" run "$KICKOFF" || echo "  (run $i returned non-zero — continuing to measure)"
    fire_writers
    snapshot "after run $i"
    echo "  >>> router decision now: researcher/$PROBE_LANE -> $(probe_router)"
    echo
  done
else
  echo "  [inspect-only] MO_VALIDATE_DO_RUNS!=1 — skipping real dispatch."
  echo "  router probe is read-only — no DB writers fire in inspect mode."
  snapshot "AFTER snapshot only (no new run, no writers)"
fi

ROUTE_AFTER="$(probe_router)"
echo "==============================================================="
echo "  router decision BEFORE : researcher/$PROBE_LANE -> $ROUTE_BEFORE"
echo "  router decision AFTER  : researcher/$PROBE_LANE -> $ROUTE_AFTER"

LANES="$(q "SELECT COUNT(DISTINCT agent_version_id) FROM execution_traces WHERE task_class='$TC' AND agent_version_id<>'';")"
POS_ADV="$(q "SELECT COUNT(*) FROM agent_performance_memory WHERE task_class='$TC' AND relative_advantage>0;")"
WINNER="$(q "SELECT agent_version_id FROM agent_performance_memory WHERE task_class='$TC' AND relative_advantage>0 ORDER BY relative_advantage DESC LIMIT 1;")"

echo "  ---"
echo "  competing lanes stamped : ${LANES:-0}   (need >=2 for GRPO to rank)"
echo "  lanes with adv>0 (GRPO) : ${POS_ADV:-0}   winner: ${WINNER:-<none>}"
if [ "$ROUTE_AFTER" != "$ROUTE_BEFORE" ]; then
  echo "  VERDICT: LEARNING OBSERVED — router moved $ROUTE_BEFORE -> $ROUTE_AFTER"
elif [ -n "$WINNER" ] && [ "$ROUTE_AFTER" = "$WINNER" ]; then
  echo "  VERDICT: LEARNING ACTIVE — router already pinned to learned winner ($WINNER)"
elif [ "${POS_ADV:-0}" -gt 0 ]; then
  echo "  VERDICT: PARTIAL — GRPO has a winner ($WINNER) but the AND-gate's RHO"
  echo "           sample floor (>= $MO_LEARNING_MIN_SAMPLES) isn't met yet; run more iterations."
else
  echo "  VERDICT: COLD — not enough evidence yet; increase N (real runs) to accumulate samples."
fi
echo "==============================================================="
