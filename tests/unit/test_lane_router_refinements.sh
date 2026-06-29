#!/usr/bin/env bash
# Unit test for the GRPO write-half refinements in lane_router_recompute_advantages
# (knobs: n-aware shrinkage, EMA decay, recency weighting, sigma-zero cost tie-break).
# Each knob is asserted to change the stored relative_advantage in the documented
# direction vs its legacy escape hatch — so the port cannot pass vacuously.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
. "$ROOT/lib/lane_router.sh"

PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
DB="$TMP/state.db"
# Base schema from the project DB (has execution_traces + agent_performance_memory).
cp "${MINI_ORK_HOME:-$ROOT/.mini-ork}/state.db" "$DB"
export MINI_ORK_DB="$DB"

# Insert one trace. args: lane reward_g cost created_at [region]
_ins(){
  local lane="$1" rg="$2" cost="$3" cat="$4" region="${5:-}"
  sqlite3 "$DB" "INSERT INTO execution_traces
    (trace_id, agent_version_id, task_class, objective_domain, code_region,
     verifier_output, reward_g, cost_usd, status, created_at)
    VALUES ('t-$RANDOM-$RANDOM','$lane','tc','code-delivery','$region',
     '{\"node_type\":\"implementer\"}', $rg, $cost, 'success', '$cat');" 2>/dev/null
}
_reset(){ sqlite3 "$DB" "DELETE FROM execution_traces; DELETE FROM agent_performance_memory;
  DELETE FROM lane_domain_advantage; DELETE FROM lane_region_advantage;" 2>/dev/null; }
_adv(){ sqlite3 "$DB" "SELECT printf('%.4f',relative_advantage) FROM agent_performance_memory WHERE agent_version_id='$1' AND task_class='tc';" 2>/dev/null; }
NOW="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
OLD="$(date -u -v-60d +%Y-%m-%dT%H:%M:%S.000Z 2>/dev/null || date -u -d '60 days ago' +%Y-%m-%dT%H:%M:%S.000Z)"

echo "── unit: lib/lane_router.sh GRPO refinements ──"

# ── Knob 1: n-aware shrinkage ──────────────────────────────────────────────
# Group A(reward_g=1.0) vs B(0.0); mean 0.5; A's raw adv = +0.5.
# SHRINK_K=5, n=1 -> factor 1/6 -> ~0.083. SHRINK_K=0 -> 0.5 (legacy).
_reset; _ins laneA 1.0 1 "$NOW"; _ins laneB 0.0 1 "$NOW"
MO_LEARNING_DECAY_ALPHA=1.0 MO_LEARNING_HALFLIFE_DAYS=0 MO_LEARNING_TIEBREAK=0 \
  MO_LEARNING_SHRINKAGE_K=5 lane_router_recompute_advantages --since 0 >/dev/null 2>&1
shrunk="$(_adv laneA)"
_reset; _ins laneA 1.0 1 "$NOW"; _ins laneB 0.0 1 "$NOW"
MO_LEARNING_DECAY_ALPHA=1.0 MO_LEARNING_HALFLIFE_DAYS=0 MO_LEARNING_TIEBREAK=0 \
  MO_LEARNING_SHRINKAGE_K=0 lane_router_recompute_advantages --since 0 >/dev/null 2>&1
raw="$(_adv laneA)"
awk "BEGIN{exit !($shrunk>0 && $shrunk<$raw && $raw>0.4)}" && ok "shrinkage pulls small-n adv down ($shrunk < $raw=raw)" || bad "shrinkage (shrunk=$shrunk raw=$raw)"

# ── Knob 4: sigma-zero cost tie-break ──────────────────────────────────────
# Flat group: both reward_g=0.5 -> adv 0 for both. cheap laneC < dear laneD cost.
# TIEBREAK=1 -> cheaper laneC adv > dearer laneD. TIEBREAK=0 -> both ~0.
_reset; _ins laneC 0.5 1 "$NOW"; _ins laneD 0.5 100 "$NOW"
MO_LEARNING_DECAY_ALPHA=1.0 MO_LEARNING_HALFLIFE_DAYS=0 MO_LEARNING_SHRINKAGE_K=0 \
  MO_LEARNING_TIEBREAK=1 lane_router_recompute_advantages --since 0 >/dev/null 2>&1
c="$(_adv laneC)"; d="$(_adv laneD)"
awk "BEGIN{exit !($c>$d)}" && ok "tie-break favors cheaper lane on flat scores (C=$c > D=$d)" || bad "tie-break (C=$c D=$d)"
_reset; _ins laneC 0.5 1 "$NOW"; _ins laneD 0.5 100 "$NOW"
MO_LEARNING_DECAY_ALPHA=1.0 MO_LEARNING_HALFLIFE_DAYS=0 MO_LEARNING_SHRINKAGE_K=0 \
  MO_LEARNING_TIEBREAK=0 lane_router_recompute_advantages --since 0 >/dev/null 2>&1
c0="$(_adv laneC)"
awk "BEGIN{exit !($c0>=-0.0001 && $c0<=0.0001)}" && ok "tie-break legacy (TIEBREAK=0) leaves flat group at 0 (C=$c0)" || bad "tie-break legacy (C=$c0)"

# ── Knob 2: EMA decay blend ────────────────────────────────────────────────
# First recompute stores batch1 for laneA; second recompute with a different
# batch blends. DECAY_ALPHA=1.0 overwrites (=batch2); 0.30 blends toward prior.
_reset; _ins laneA 1.0 1 "$NOW"; _ins laneB 0.0 1 "$NOW"
MO_LEARNING_SHRINKAGE_K=0 MO_LEARNING_HALFLIFE_DAYS=0 MO_LEARNING_TIEBREAK=0 \
  MO_LEARNING_DECAY_ALPHA=1.0 lane_router_recompute_advantages --since 0 >/dev/null 2>&1
sqlite3 "$DB" "DELETE FROM execution_traces;" 2>/dev/null
_ins laneA 0.0 1 "$NOW"; _ins laneB 1.0 1 "$NOW"   # now laneA's batch adv = -0.5
MO_LEARNING_SHRINKAGE_K=0 MO_LEARNING_HALFLIFE_DAYS=0 MO_LEARNING_TIEBREAK=0 \
  MO_LEARNING_DECAY_ALPHA=0.30 lane_router_recompute_advantages --since 0 >/dev/null 2>&1
blended="$(_adv laneA)"   # expect 0.30*(-0.5)+0.70*(+0.5)=+0.20
awk "BEGIN{exit !($blended>0.1 && $blended<0.3)}" && ok "EMA blends toward prior (laneA=$blended, expect ~0.20)" || bad "EMA blend (laneA=$blended)"

# ── Knob 3: recency weighting ──────────────────────────────────────────────
# Same lane, fresh strong-positive + old strong-negative trace. With HALFLIFE=14
# the old negative is down-weighted, so the stored advantage is higher than with
# HALFLIFE=0 (equal weight). Use a competitor so the group isn't degenerate.
_reset; _ins laneA 1.0 1 "$NOW"; _ins laneA 0.0 1 "$OLD"; _ins laneB 0.5 1 "$NOW"
MO_LEARNING_SHRINKAGE_K=0 MO_LEARNING_DECAY_ALPHA=1.0 MO_LEARNING_TIEBREAK=0 \
  MO_LEARNING_HALFLIFE_DAYS=14 lane_router_recompute_advantages --since 0 >/dev/null 2>&1
recent_w="$(_adv laneA)"
_reset; _ins laneA 1.0 1 "$NOW"; _ins laneA 0.0 1 "$OLD"; _ins laneB 0.5 1 "$NOW"
MO_LEARNING_SHRINKAGE_K=0 MO_LEARNING_DECAY_ALPHA=1.0 MO_LEARNING_TIEBREAK=0 \
  MO_LEARNING_HALFLIFE_DAYS=0 lane_router_recompute_advantages --since 0 >/dev/null 2>&1
flat_w="$(_adv laneA)"
awk "BEGIN{exit !($recent_w>$flat_w)}" && ok "recency up-weights fresh trace (halflife14=$recent_w > halflife0=$flat_w)" || bad "recency (h14=$recent_w h0=$flat_w)"

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
