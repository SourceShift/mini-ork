#!/usr/bin/env bash
# Integration smoke for the meta-orchestrator compound loop.
#
# Scope: only the components shipped on this branch
# (feat/operator-steering-injection):
#   - epic_graph: epic_dependencies + ready_now + cascade
#   - bug_report channel (lib/bug_report.sh)
#   - topology + role_evolver (Phase 1 of meta-orchestrator)
#   - mini-ork-conductor (Phase 2)
#   - mini-ork-lifetime (Phase 3)
#
# Skips checks for libs that live on other branches (similarity.sh,
# rho_aggregator.sh, lane_router.sh, process_reward.sh) but still
# exercises the conductor's read-path, which queries the tables those
# libs populate — pre-populated rows from earlier real runs are enough.

set -o pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
export MINI_ORK_ROOT
MINI_ORK_HOME="${MINI_ORK_HOME:-$MINI_ORK_ROOT/.mini-ork}"
export MINI_ORK_HOME
STATE_DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"
MINI_ORK_DB="$STATE_DB"
export STATE_DB MINI_ORK_DB

PASS=0; FAIL=0
_t() { printf '\n=== %s ===\n' "$1"; }
_check() {
  local desc="$1" actual="$2" expected="$3"
  if [ "$actual" = "$expected" ]; then
    printf "  PASS  %s\n" "$desc"; PASS=$((PASS+1))
  else
    printf "  FAIL  %s\n    expected=%q\n    actual=%q\n" "$desc" "$expected" "$actual"
    FAIL=$((FAIL+1))
  fi
}
_check_ge() {
  local desc="$1" actual="$2" min="$3"
  if [ "$actual" -ge "$min" ] 2>/dev/null; then
    printf "  PASS  %s (got %s, min %s)\n" "$desc" "$actual" "$min"
    PASS=$((PASS+1))
  else
    printf "  FAIL  %s\n    expected >= %s\n    actual = %s\n" "$desc" "$min" "$actual"
    FAIL=$((FAIL+1))
  fi
}

TS=$$
ROOT_EPIC="mol-root-$TS"
DEP_EPIC="mol-dep-$TS"
SEED_TC="mol_test_class_$TS"
SEED_WF="mol_wf_$TS"
SEED_LANE="mol_lane_$TS"
SEED_RUN="mol-run-$TS"

_cleanup() {
  sqlite3 "$STATE_DB" "DELETE FROM epic_dependencies WHERE from_epic_id LIKE 'mol-%-$TS' OR to_epic_id LIKE 'mol-%-$TS';" 2>/dev/null || true
  sqlite3 "$STATE_DB" "UPDATE epics SET archived_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id LIKE 'mol-%-$TS';" 2>/dev/null || true
  sqlite3 "$STATE_DB" "DELETE FROM epics WHERE id LIKE 'mol-%-$TS' AND status != 'done';" 2>/dev/null || true
  sqlite3 "$STATE_DB" "DELETE FROM execution_traces WHERE trace_id LIKE 'mol-tr-%-$TS';" 2>/dev/null || true
  sqlite3 "$STATE_DB" "DELETE FROM topology_win_rates WHERE task_class='$SEED_TC';" 2>/dev/null || true
  sqlite3 "$STATE_DB" "DELETE FROM conductor_decisions WHERE epic_id LIKE 'mol-%-$TS';" 2>/dev/null || true
}
trap _cleanup EXIT

_t "1. Seed root + dependent epic via epic_dependencies"
sqlite3 "$STATE_DB" \
  "INSERT INTO epics(id, title, status) VALUES('$ROOT_EPIC','meta-orch smoke root','not started');
   INSERT INTO epics(id, title, status) VALUES('$DEP_EPIC','meta-orch smoke dep','blocked');
   INSERT INTO epic_dependencies(from_epic_id, to_epic_id, kind)
     VALUES('$ROOT_EPIC','$DEP_EPIC','hard');"
N=$(sqlite3 "$STATE_DB" "SELECT COUNT(*) FROM epics WHERE id LIKE 'mol-%-$TS';")
_check "2 epics seeded" "$N" "2"

_t "2. epic_graph_ready_now picks only the unblocked root"
# shellcheck source=lib/epic_graph.sh
source "$MINI_ORK_ROOT/lib/epic_graph.sh"
READY=$(epic_graph_ready_now | grep "^mol-" || true)
_check "ready_now lists only the seeded root" "$READY" "$ROOT_EPIC"

_t "3. epic_graph_on_done cascades dep unblock"
sqlite3 "$STATE_DB" "UPDATE epics SET status='done' WHERE id='$ROOT_EPIC';"
epic_graph_on_done "$ROOT_EPIC"
DEP_STATUS=$(sqlite3 "$STATE_DB" "SELECT status FROM epics WHERE id='$DEP_EPIC';")
_check "dependent epic flipped 'blocked' -> 'not started'" "$DEP_STATUS" "not started"

_t "4. Seed execution_traces and recompute topology win-rates"
for n in 1 2 3 4; do
  status="success"; verdict="APPROVE"
  [ "$n" -gt 2 ] && status="failure" && verdict="REJECT"
  sqlite3 "$STATE_DB" \
    "INSERT INTO execution_traces(trace_id, run_id, task_class,
        workflow_version_id, agent_version_id, status, reviewer_verdict,
        verifier_output, cost_usd, duration_ms, created_at)
     VALUES('mol-tr-$n-$TS','$SEED_RUN','$SEED_TC',
            '$SEED_WF','$SEED_LANE','$status','$verdict',
            '{\"node_type\":\"researcher\"}',0.05,1500,
            strftime('%Y-%m-%dT%H:%M:%S.000Z','now'));"
done
# shellcheck source=lib/topology.sh
source "$MINI_ORK_ROOT/lib/topology.sh"
topology_recompute_win_rates --since 0 >/dev/null
WR=$(sqlite3 "$STATE_DB" "SELECT printf('%.2f', win_rate) FROM topology_win_rates WHERE task_class='$SEED_TC';")
_check "topology win_rate = 0.50 (2 wins / 4 traces)" "$WR" "0.50"

_t "5. role_evolver_propose runs cleanly"
# shellcheck source=lib/role_evolver.sh
source "$MINI_ORK_ROOT/lib/role_evolver.sh"
N=$(role_evolver_propose --top 3 2>/dev/null)
if printf '%s' "${N}" | grep -qE '^[0-9]+$'; then
  printf "  PASS  role_evolver_propose returned numeric count (n=%s)\n" "$N"
  PASS=$((PASS+1))
else
  printf "  FAIL  role_evolver_propose unexpected output: %q\n" "$N"
  FAIL=$((FAIL+1))
fi

_t "6. conductor --once --dry-run --explain emits JSON decision"
DEC=$(MO_PLASTICITY_BUDGET=5 \
  "$MINI_ORK_ROOT/bin/mini-ork-conductor" --once --dry-run --explain 2>&1 || true)
echo "$DEC" | head -8 | sed 's/^/    /'
echo "$DEC" | grep -q '"epic_id"' && OK=yes || OK=no
_check "conductor emitted a decision JSON with epic_id" "$OK" "yes"
ROWS=$(sqlite3 "$STATE_DB" "SELECT COUNT(*) FROM conductor_decisions WHERE decided_at >= strftime('%s','now','-2 minutes');")
_check_ge "conductor_decisions appended >= 1 row in last 2min" "${ROWS:-0}" "1"

_t "7. lifetime summary prints non-empty leaderboards"
OUT=$("$MINI_ORK_ROOT/bin/mini-ork-lifetime" summary 2>&1)
echo "$OUT" | grep -q 'Run volume' && OK=yes || OK=no
_check "lifetime summary has Run volume section" "$OK" "yes"
echo "$OUT" | grep -q 'topologies by win_rate' && OK=yes || OK=no
_check "lifetime summary has topologies leaderboard" "$OK" "yes"

printf '\n=== SUMMARY: %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
