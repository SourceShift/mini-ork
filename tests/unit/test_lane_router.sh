#!/usr/bin/env bash
# tests/unit/test_lane_router.sh — unit tests for frc-a5 delayed-penalty fold
# in lib/lane_router.sh::lane_router_recompute_advantages.
#
# Verifies, against an isolated temp SQLite DB:
#   1. baseline   — no defect_attributions row → blamed lane advantage
#                    equals its no-penalty baseline.
#   2. drop       — fresh defect_attributions row with negative penalty
#                    reduces the blamed lane's lane_region_advantage
#                    relative to baseline.
#   3. decay      — an older penalty with the same magnitude produces a
#                    smaller drop than the fresh penalty, consistent with
#                    `decay = 0.5 ** (age_days / decay_halflife_days)`.
#   4. unblamed   — a lane without any defect_attributions row keeps its
#                    lane_region_advantage unchanged vs. the baseline.
#   5. isolation  — penalty for (lane, code_region=A) does not leak into
#                    (lane, code_region=B) for the same lane.
#
# Also verifies the global and per-domain slices are NOT penalized even
# when a defect_attributions row exists (region-scoped by design).
#
# Run: bash tests/unit/test_lane_router.sh
# Exit 0 on all green, non-zero otherwise.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: lib/lane_router.sh (frc-a5 delayed-penalty fold) ──"

LIB="$MINI_ORK_ROOT/lib/lane_router.sh"
if [ ! -f "$LIB" ]; then
  _skip "lib/lane_router.sh not found"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated temp DB + state.
TEST_HOME=$(mktemp -d)
export MINI_ORK_HOME="$TEST_HOME"
export MINI_ORK_DB="$TEST_HOME/state.db"
trap 'rm -rf "$TEST_HOME"' EXIT

# Apply minimum migrations. Order matters: execution_traces (0010/0014),
# agent_performance_memory (0009), reward column (0042), region tables
# (0043/0044), defect_attributions (0045).
for m in \
  db/migrations/0009_memory_namespaces.sql \
  db/migrations/0010_benchmarks.sql \
  db/migrations/0014_execution_traces_relax_fk_and_status.sql \
  db/migrations/0042_execution_traces_objective_aware_reward.sql \
  db/migrations/0043_lane_domain_advantage.sql \
  db/migrations/0044_execution_traces_code_region.sql \
  db/migrations/0045_defect_attributions.sql ; do
  if ! MINI_ORK_DB="$MINI_ORK_DB" sqlite3 "$MINI_ORK_DB" < "$MINI_ORK_ROOT/$m" 2>/dev/null; then
    _skip "migration $m failed to apply"
    echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
    exit 0
  fi
done

# Source the lib under test. mo_store_assert_sqlite must resolve a
# SQLite backend; policy_store defaults to sqlite when MO_STORE_BACKEND
# is unset.
unset MO_STORE_BACKEND MO_STORE_DB
# shellcheck disable=SC1091
. "$LIB"

# Helper: insert 4 execution_traces in the same group
# (objective_domain=code-delivery, task_class=code-fix, node_type=implementer)
# with code_region=regionA: codex_lens wins 2/2, kimi_lens loses 2/2.
# mean reward_g = 0.5, so codex advantage = +0.45, kimi advantage = -0.45.
_seed_regionA_traces() {
  sqlite3 "$MINI_ORK_DB" <<SQL
INSERT INTO execution_traces
  (trace_id, run_id, workflow_version_id, agent_version_id, task_class,
   prompt_version_hash, context_bundle_hash, tool_calls, files_read,
   files_written, verifier_output, reviewer_verdict, cost_usd,
   duration_ms, final_artifact_ref, status, process_reward,
   objective_domain, reward_g, reward_direction, reward_source, validity,
   code_region, created_at)
VALUES
  ('t-codex-1', NULL, 'wf', 'codex_lens', 'code-fix',
   'p', 'ctx', '[]', '[]', '[]', '{"node_type":"implementer"}',
   'APPROVE', 0.05, 200, 'artifact', 'success', 0.95,
   'code-delivery', 0.95, 'higher_is_better', 'verifier@v1', 'valid',
   'regionA', '2026-06-01T00:00:00.000Z'),
  ('t-codex-2', NULL, 'wf', 'codex_lens', 'code-fix',
   'p', 'ctx', '[]', '[]', '[]', '{"node_type":"implementer"}',
   'APPROVE', 0.05, 200, 'artifact', 'success', 0.95,
   'code-delivery', 0.95, 'higher_is_better', 'verifier@v1', 'valid',
   'regionA', '2026-06-01T00:00:00.000Z'),
  ('t-kimi-1',  NULL, 'wf', 'kimi_lens', 'code-fix',
   'p', 'ctx', '[]', '[]', '[]', '{"node_type":"implementer"}',
   'REJECT', 0.01, 200, 'artifact', 'failure', 0.05,
   'code-delivery', 0.05, 'higher_is_better', 'verifier@v1', 'valid',
   'regionA', '2026-06-01T00:00:00.000Z'),
  ('t-kimi-2',  NULL, 'wf', 'kimi_lens', 'code-fix',
   'p', 'ctx', '[]', '[]', '[]', '{"node_type":"implementer"}',
   'REJECT', 0.01, 200, 'artifact', 'failure', 0.05,
   'code-delivery', 0.05, 'higher_is_better', 'verifier@v1', 'valid',
   'regionA', '2026-06-01T00:00:00.000Z');
SQL
}

# Helper: insert 4 execution_traces for the SAME lanes in regionB so we
# can verify no-leakage across regions.
_seed_regionB_traces() {
  sqlite3 "$MINI_ORK_DB" <<SQL
INSERT INTO execution_traces
  (trace_id, run_id, workflow_version_id, agent_version_id, task_class,
   prompt_version_hash, context_bundle_hash, tool_calls, files_read,
   files_written, verifier_output, reviewer_verdict, cost_usd,
   duration_ms, final_artifact_ref, status, process_reward,
   objective_domain, reward_g, reward_direction, reward_source, validity,
   code_region, created_at)
VALUES
  ('t-codex-1b', NULL, 'wf', 'codex_lens', 'code-fix',
   'p', 'ctx', '[]', '[]', '[]', '{"node_type":"implementer"}',
   'APPROVE', 0.05, 200, 'artifact', 'success', 0.95,
   'code-delivery', 0.95, 'higher_is_better', 'verifier@v1', 'valid',
   'regionB', '2026-06-01T00:00:00.000Z'),
  ('t-codex-2b', NULL, 'wf', 'codex_lens', 'code-fix',
   'p', 'ctx', '[]', '[]', '[]', '{"node_type":"implementer"}',
   'APPROVE', 0.05, 200, 'artifact', 'success', 0.95,
   'code-delivery', 0.95, 'higher_is_better', 'verifier@v1', 'valid',
   'regionB', '2026-06-01T00:00:00.000Z'),
  ('t-kimi-1b',  NULL, 'wf', 'kimi_lens', 'code-fix',
   'p', 'ctx', '[]', '[]', '[]', '{"node_type":"implementer"}',
   'REJECT', 0.01, 200, 'artifact', 'failure', 0.05,
   'code-delivery', 0.05, 'higher_is_better', 'verifier@v1', 'valid',
   'regionB', '2026-06-01T00:00:00.000Z'),
  ('t-kimi-2b',  NULL, 'wf', 'kimi_lens', 'code-fix',
   'p', 'ctx', '[]', '[]', '[]', '{"node_type":"implementer"}',
   'REJECT', 0.01, 200, 'artifact', 'failure', 0.05,
   'code-delivery', 0.05, 'higher_is_better', 'verifier@v1', 'valid',
   'regionB', '2026-06-01T00:00:00.000Z');
SQL
}

# Helper: insert a defect_attributions row with the given age in days and
# half-life. ts is computed relative to "now" so decay is reproducible.
_insert_defect_attr() {
  local lane="$1" region="$2" tc="$3"
  local penalty="$4" half_life="$5" age_days="$6"
  local iso_ts
  iso_ts="$(python3 - "$age_days" <<'PY'
import datetime, sys
days = float(sys.argv[1])
ts = datetime.datetime.utcnow() - datetime.timedelta(days=days)
print(ts.strftime('%Y-%m-%dT%H:%M:%fZ'))
PY
  )"
  sqlite3 "$MINI_ORK_DB" <<SQL
INSERT INTO defect_attributions
  (found_run_id, blamed_run_id, lane, code_region, task_class,
   severity, penalty, decay_halflife_days, ts)
VALUES
  ('run-found-$RANDOM', 'run-blamed-$RANDOM', '$lane', '$region', '$tc',
   'high', $penalty, $half_life, '$iso_ts');
SQL
}

# Helper: read the lane_region_advantage row for (lane, code_region, tc).
_region_adv() {
  local lane="$1" region="$2" tc="$3"
  sqlite3 "$MINI_ORK_DB" \
    "SELECT printf('%.4f', relative_advantage)
       FROM lane_region_advantage
      WHERE agent_version_id='$lane'
        AND code_region='$region'
        AND task_class='$tc';"
}

# Helper: read the per-domain lane_domain_advantage row.
_domain_adv() {
  local lane="$1" od="$2" tc="$3"
  sqlite3 "$MINI_ORK_DB" \
    "SELECT printf('%.4f', relative_advantage)
       FROM lane_domain_advantage
      WHERE agent_version_id='$lane'
        AND objective_domain='$od'
        AND task_class='$tc';"
}

# Helper: read the global agent_performance_memory row.
_global_adv() {
  local lane="$1" tc="$2"
  sqlite3 "$MINI_ORK_DB" \
    "SELECT printf('%.4f', relative_advantage)
       FROM agent_performance_memory
      WHERE agent_version_id='$lane'
        AND task_class='$tc';"
}

# ── Phase 1: no-penalty baseline (regionA + regionB) ────────────────────

_seed_regionA_traces
_seed_regionB_traces

if ! lane_router_recompute_advantages >/dev/null 2>&1; then
  _fail "lane_router_recompute_advantages (baseline) exited non-zero"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 1
fi

BASE_CODEX_A=$(_region_adv codex_lens regionA code-fix)
BASE_KIMI_A=$(_region_adv  kimi_lens  regionA code-fix)
BASE_CODEX_B=$(_region_adv codex_lens regionB code-fix)
BASE_KIMI_B=$(_region_adv  kimi_lens  regionB code-fix)
BASE_CODEX_DOM=$(_domain_adv codex_lens code-delivery code-fix)
BASE_KIMI_DOM=$(_domain_adv  kimi_lens  code-delivery code-fix)
BASE_CODEX_GL=$(_global_adv codex_lens code-fix)
BASE_KIMI_GL=$(_global_adv  kimi_lens  code-fix)

# Sanity: no-penalty recompute must yield codex = +0.9, kimi = -0.9
# (avg over n=2 traces, adv_sum = 2 * (score - mean)).
[ -n "$BASE_CODEX_A" ] && _ok "baseline: codex_lens regionA row exists ($BASE_CODEX_A)" \
  || _fail "baseline: codex_lens regionA row missing"
[ -n "$BASE_KIMI_A" ] && _ok "baseline: kimi_lens regionA row exists ($BASE_KIMI_A)" \
  || _fail "baseline: kimi_lens regionA row missing"

# ── Phase 2: fresh penalty on codex_lens regionA ─────────────────────────

# Fresh (age=0 days) penalty = -0.5, halflife = 30 days → decay ≈ 1.0
# → expected effective ≈ -0.5 → codex advantage drops from +0.9 to ~+0.4.
_insert_defect_attr codex_lens regionA code-fix -0.5 30.0 0.0

if ! lane_router_recompute_advantages >/dev/null 2>&1; then
  _fail "lane_router_recompute_advantages (after fresh penalty) exited non-zero"
fi

AFTER_FRESH_CODEX_A=$(_region_adv codex_lens regionA code-fix)
AFTER_FRESH_KIMI_A=$(_region_adv  kimi_lens  regionA code-fix)

# Blamed lane advantage should drop in regionA.
if python3 -c "
import sys
base = float('$BASE_CODEX_A')
after = float('$AFTER_FRESH_CODEX_A')
sys.exit(0 if after < base else 1)
" 2>/dev/null; then
  _ok "fresh penalty: codex_lens regionA drops ($BASE_CODEX_A → $AFTER_FRESH_CODEX_A)"
else
  _fail "fresh penalty: codex_lens regionA did NOT drop ($BASE_CODEX_A → $AFTER_FRESH_CODEX_A)"
fi

# Unblamed lane (kimi) in the same region must be UNCHANGED.
if [ "$BASE_KIMI_A" = "$AFTER_FRESH_KIMI_A" ]; then
  _ok "unblamed lane (kimi) regionA unchanged ($BASE_KIMI_A = $AFTER_FRESH_KIMI_A)"
else
  _fail "unblamed lane (kimi) regionA CHANGED ($BASE_KIMI_A → $AFTER_FRESH_KIMI_A)"
fi

# Same lane in regionB must be UNCHANGED (no region leakage).
AFTER_FRESH_CODEX_B=$(_region_adv codex_lens regionB code-fix)
AFTER_FRESH_KIMI_B=$(_region_adv  kimi_lens  regionB code-fix)
if [ "$BASE_CODEX_B" = "$AFTER_FRESH_CODEX_B" ] && [ "$BASE_KIMI_B" = "$AFTER_FRESH_KIMI_B" ]; then
  _ok "no region leakage: codex_lens regionB unchanged ($BASE_CODEX_B); kimi_lens regionB unchanged ($BASE_KIMI_B)"
else
  _fail "region leakage: regionB changed (codex $BASE_CODEX_B→$AFTER_FRESH_CODEX_B, kimi $BASE_KIMI_B→$AFTER_FRESH_KIMI_B)"
fi

# Global + per-domain slices must NOT be touched by region penalty.
AFTER_FRESH_CODEX_DOM=$(_domain_adv codex_lens code-delivery code-fix)
AFTER_FRESH_KIMI_DOM=$(_domain_adv  kimi_lens  code-delivery code-fix)
AFTER_FRESH_CODEX_GL=$(_global_adv codex_lens code-fix)
AFTER_FRESH_KIMI_GL=$(_global_adv  kimi_lens  code-fix)
if [ "$BASE_CODEX_DOM" = "$AFTER_FRESH_CODEX_DOM" ] \
   && [ "$BASE_KIMI_DOM" = "$AFTER_FRESH_KIMI_DOM" ] \
   && [ "$BASE_CODEX_GL" = "$AFTER_FRESH_CODEX_GL" ] \
   && [ "$BASE_KIMI_GL" = "$AFTER_FRESH_KIMI_GL" ]; then
  _ok "global + per-domain slices untouched by region penalty"
else
  _fail "global/domain leaked: dom($BASE_CODEX_DOM,$BASE_KIMI_DOM → $AFTER_FRESH_CODEX_DOM,$AFTER_FRESH_KIMI_DOM); gl($BASE_CODEX_GL,$BASE_KIMI_GL → $AFTER_FRESH_CODEX_GL,$AFTER_FRESH_KIMI_GL)"
fi

# ── Phase 3: insert an older penalty with same magnitude ─────────────────

# Halflife=30, age=30 days → decay = 0.5. Older penalty's effective
# magnitude is half the fresh one, so its impact on advantage is smaller.

# Capture current state before adding the aged penalty.
PRE_AGED_CODEX_A=$(_region_adv codex_lens regionA code-fix)

_insert_defect_attr codex_lens regionA code-fix -0.5 30.0 30.0

if ! lane_router_recompute_advantages >/dev/null 2>&1; then
  _fail "lane_router_recompute_advantages (after aged penalty) exited non-zero"
fi

AFTER_AGED_CODEX_A=$(_region_adv codex_lens regionA code-fix)

# After-aged advantage must be lower than pre-aged (fresh penalty alone),
# because the aged penalty contributes another negative ~0.25.
# And it must be higher than a hypothetical full-fresh (-1.0) fold.
if python3 -c "
import sys
pre = float('$PRE_AGED_CODEX_A')
after = float('$AFTER_AGED_CODEX_A')
# aged penalty = -0.25 (halved). New advantage ≈ pre + (-0.25) = pre - 0.25
# We allow a small float tolerance (rounded to 4 dp).
sys.exit(0 if abs((pre - after) - 0.25) < 0.0005 else 1)
" 2>/dev/null; then
  _ok "aged penalty (halflife=30d, age=30d) halves the contribution: pre=$PRE_AGED_CODEX_A → after=$AFTER_AGED_CODEX_A"
else
  _fail "aged penalty not decayed correctly: pre=$PRE_AGED_CODEX_A → after=$AFTER_AGED_CODEX_A (expected ~0.25 drop)"
fi

# The fresh-penalty drop (0.9 → 0.4) must be larger in magnitude than
# the aged-penalty incremental drop (0.4 → ~0.15).
FRESH_DROP=$(python3 -c "print('%.4f' % (float('$BASE_CODEX_A') - float('$PRE_AGED_CODEX_A')))")
AGED_DROP=$(python3 -c "print('%.4f' % (float('$PRE_AGED_CODEX_A') - float('$AFTER_AGED_CODEX_A')))")
if python3 -c "
import sys
fresh = abs(float('$FRESH_DROP'))
aged = abs(float('$AGED_DROP'))
sys.exit(0 if aged < fresh else 1)
" 2>/dev/null; then
  _ok "decay ordering: fresh drop ($FRESH_DROP) > aged drop ($AGED_DROP)"
else
  _fail "decay ordering violated: fresh drop ($FRESH_DROP) ≤ aged drop ($AGED_DROP)"
fi

# ── Phase 4: malformed halflife is skipped (not silently invented) ───────

# Insert a row with halflife = 0 — must be skipped without crashing.
sqlite3 "$MINI_ORK_DB" <<SQL
INSERT INTO defect_attributions
  (found_run_id, blamed_run_id, lane, code_region, task_class,
   severity, penalty, decay_halflife_days, ts)
VALUES
  ('run-malformed', 'run-blamed', 'kimi_lens', 'regionA', 'code-fix',
   'low', -0.9, 0.0, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
SQL

if lane_router_recompute_advantages >/dev/null 2>&1; then
  AFTER_MALFORMED_KIMI_A=$(_region_adv kimi_lens regionA code-fix)
  if [ "$AFTER_MALFORMED_KIMI_A" = "$BASE_KIMI_A" ]; then
    _ok "malformed halflife (0.0) skipped — kimi_lens regionA unchanged ($BASE_KIMI_A)"
  else
    _fail "malformed halflife unexpectedly applied: kimi_lens regionA $BASE_KIMI_A → $AFTER_MALFORMED_KIMI_A"
  fi
else
  _fail "lane_router_recompute_advantages crashed on malformed halflife row"
fi

# ── Done ──────────────────────────────────────────────────────────────────

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ]
