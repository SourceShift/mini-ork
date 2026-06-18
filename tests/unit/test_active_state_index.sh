#!/usr/bin/env bash
# tests/unit/test_active_state_index.sh — unit tests for
# lib/active_state_index.sh (HarnessBridge Technique 1, arxiv:2606.12882).
#
# Covers:
#   - empty DB returns block with at least decision_variables section
#   - seeded failure_memory surfaces unresolved_errors
#   - seeded policy_decisions with DENY surfaces open_constraints
#   - seeded task_runs with APPROVE verdict surfaces established_facts
#   - seeded task_runs with executing status surfaces pending_goals
#   - MO_DISABLE_ACTIVE_STATE=1 short-circuits
#   - block is valid JSON when extracted from the markdown wrapper

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/active_state_index.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: lib/active_state_index.sh ──"

if [ ! -f "$LIB" ]; then
  _skip "lib/active_state_index.sh not found"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TEST_HOME=$(mktemp -d)
export MINI_ORK_HOME="$TEST_HOME"
export MINI_ORK_DB="$TEST_HOME/state.db"
trap 'rm -rf "$TEST_HOME"' EXIT

# Apply minimum migrations.
for m in db/migrations/0009_memory_namespaces.sql \
         db/migrations/0013_task_runs.sql \
         db/migrations/0026_policy_state.sql; do
  [ -f "$MINI_ORK_ROOT/$m" ] && sqlite3 "$MINI_ORK_DB" < "$MINI_ORK_ROOT/$m" 2>/dev/null || true
done
# failure_memory FK requires runs table; stub it.
sqlite3 "$MINI_ORK_DB" "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY);" 2>/dev/null
sqlite3 "$MINI_ORK_DB" "INSERT OR IGNORE INTO runs (id) VALUES (1);" 2>/dev/null

# shellcheck disable=SC1090
source "$LIB"

# Case 1: empty DB still produces a block with decision_variables.
out=$(mo_active_state_block "__any__" 30)
if printf '%s' "$out" | grep -q '"decision_variables"' && \
   printf '%s' "$out" | grep -q "ACTIVE STATE INDEX"; then
  _ok "empty DB produces block with decision_variables"
else
  _fail "empty DB block missing expected sections"
fi

# Case 2: extracted JSON is valid.
json=$(printf '%s' "$out" | awk '/^```json$/{flag=1; next} /^```$/{flag=0} flag' )
if printf '%s' "$json" | python3 -c "import sys,json; json.loads(sys.stdin.read()); print('ok')" 2>/dev/null | grep -q ok; then
  _ok "embedded JSON is valid"
else
  _fail "embedded JSON failed to parse"
fi

# Case 3: failure_memory row surfaces.
sqlite3 "$MINI_ORK_DB" "INSERT INTO failure_memory (failure_id, run_id, workflow_stage, failure_category, error_message) VALUES ('fm-unit-1', 1, 'reviewer', 'verifier_fail', 'lens-glm.md absent — TW-3');"
out=$(mo_active_state_block "__any__" 30)
if printf '%s' "$out" | grep -q 'fm-unit-1'; then
  _ok "failure_memory row surfaces as unresolved_error"
else
  _fail "failure_memory not in block"
fi

# Case 4: policy_decisions DENY row surfaces.
sqlite3 "$MINI_ORK_DB" "INSERT INTO policy_decisions (decision_id, run_id, event_type, policy_name, result, reason) VALUES ('pd-unit-1', 'run-x', 'constraint_safety', 'no_unsandboxed_dispatch', 'DENY', 'sandbox CLI absent');"
out=$(mo_active_state_block "__any__" 30)
if printf '%s' "$out" | grep -q 'pd-unit-1'; then
  _ok "policy_decisions DENY surfaces as open_constraint"
else
  _fail "policy_decisions not in block"
fi

# Case 5: REQUIRE_APPROVAL also surfaces.
sqlite3 "$MINI_ORK_DB" "INSERT INTO policy_decisions (decision_id, run_id, event_type, policy_name, result, reason) VALUES ('pd-unit-2', 'run-y', 'constraint_cost', 'per_run_cost_cap', 'REQUIRE_APPROVAL', 'projected spend exceeds cap');"
out=$(mo_active_state_block "__any__" 30)
if printf '%s' "$out" | grep -q 'pd-unit-2'; then
  _ok "policy_decisions REQUIRE_APPROVAL surfaces as open_constraint"
else
  _fail "REQUIRE_APPROVAL not in block"
fi

# Case 6: task_runs APPROVE surfaces.
sqlite3 "$MINI_ORK_DB" "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, verdict, created_at, updated_at, ended_at) VALUES ('run-est-unit', 'code_fix', 'code-fix', '/tmp/k.md', 'published', 'APPROVE', strftime('%s','now')-3600, strftime('%s','now')-3000, strftime('%s','now')-3000);"
out=$(mo_active_state_block "code_fix" 30)
if printf '%s' "$out" | grep -q 'run-est-unit'; then
  _ok "task_runs APPROVE surfaces as established_fact"
else
  _fail "task_runs APPROVE not in block"
fi

# Case 7: task_runs in-flight surfaces.
sqlite3 "$MINI_ORK_DB" "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, created_at, updated_at) VALUES ('run-pend-unit', 'code_fix', 'code-fix', '/tmp/p.md', 'executing', strftime('%s','now'), strftime('%s','now'));"
out=$(mo_active_state_block "code_fix" 30)
if printf '%s' "$out" | grep -q 'run-pend-unit'; then
  _ok "task_runs executing surfaces as pending_goal"
else
  _fail "task_runs pending not in block"
fi

# Case 8: disabled flag short-circuits.
out=$(MO_DISABLE_ACTIVE_STATE=1 mo_active_state_block "code_fix" 30)
if [ -z "$out" ]; then
  _ok "MO_DISABLE_ACTIVE_STATE=1 produces no output"
else
  _fail "disabled flag did not short-circuit"
fi

# Case 9: task_class filter.
sqlite3 "$MINI_ORK_DB" "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, verdict, created_at, updated_at, ended_at) VALUES ('run-other', 'audit', 'refactor-audit', '/tmp/a.md', 'published', 'APPROVE', strftime('%s','now')-3600, strftime('%s','now')-3000, strftime('%s','now')-3000);"
out=$(mo_active_state_block "code_fix" 30)
if printf '%s' "$out" | grep -q 'run-est-unit' && ! printf '%s' "$out" | grep -q 'run-other'; then
  _ok "task_class filter excludes non-matching runs"
else
  _fail "task_class filter broken"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" = "0" ] || exit 1
exit 0
