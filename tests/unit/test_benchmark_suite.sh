#!/usr/bin/env bash
# tests/unit/test_benchmark_suite.sh — unit tests for lib/benchmark_suite.sh
# Usage: bash tests/unit/test_benchmark_suite.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/benchmark_suite.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: benchmark_suite.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/benchmark_suite.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB
TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Apply migrations so libs without _ensure_table helpers (post-D-039) work
# in isolation, and libs that DO have _ensure_table get a clean canonical
# schema rather than racing on first-call DDL.
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations || { echo "skip: migrations failed to apply"; exit 0; }

# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: benchmark_add inserts a task and returns its id ---"

BID="$(benchmark_add '{"id":"bt-001","task_class":"unit-test","input":{"x":1},"baseline_utility_score":0.5}' 2>/dev/null)"
_assert_eq "benchmark_add returns the task id" "$BID" "bt-001"

ROW="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM benchmark_tasks WHERE benchmark_id='bt-001';" 2>/dev/null || echo 0)"
_assert_eq "benchmark_add writes row to DB" "$ROW" "1"

echo ""
echo "--- happy path: benchmark_list returns added tasks ---"

benchmark_add '{"id":"bt-002","task_class":"unit-test","baseline_utility_score":0.3}' >/dev/null 2>&1
LIST="$(benchmark_list 2>/dev/null)"
LIST_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$LIST" 2>/dev/null || echo 0)"
if [[ "$LIST_COUNT" -ge 2 ]]; then
  _ok "benchmark_list returns at least 2 tasks"
else
  _fail "benchmark_list returned $LIST_COUNT tasks (expected >=2)"
fi

echo ""
echo "--- happy path: benchmark_list --task-class filters correctly ---"

benchmark_add '{"id":"bt-other","task_class":"other-class"}' >/dev/null 2>&1
FILTERED="$(benchmark_list --task-class unit-test 2>/dev/null)"
FILTERED_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$FILTERED" 2>/dev/null || echo 0)"
OTHER_FILTERED="$(benchmark_list --task-class other-class 2>/dev/null)"
OTHER_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$OTHER_FILTERED" 2>/dev/null || echo 0)"

if [[ "$FILTERED_COUNT" -ge 2 && "$OTHER_COUNT" -eq 1 ]]; then
  _ok "benchmark_list --task-class isolates by task class"
else
  _fail "benchmark_list filter wrong: unit-test=$FILTERED_COUNT, other-class=$OTHER_COUNT"
fi

echo ""
echo "--- happy path: benchmark_run with no runner marks tasks as skipped ---"

# No MINI_ORK_WORKFLOW_RUNNER_FN set — expect skipped, not error
unset MINI_ORK_WORKFLOW_RUNNER_FN 2>/dev/null || true
RUN_OUTPUT="$(benchmark_run "cand-001" 2>/dev/null)"

if python3 -c "import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if 'total_tasks' in d else 1)" "$RUN_OUTPUT" 2>/dev/null; then
  _ok "benchmark_run returns JSON with total_tasks field"
else
  _fail "benchmark_run output not valid JSON with total_tasks: $RUN_OUTPUT"
fi

TOTAL="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['total_tasks'])" "$RUN_OUTPUT" 2>/dev/null || echo 0)"
if [[ "$TOTAL" -ge 3 ]]; then
  _ok "benchmark_run processed $TOTAL tasks (all 3 were available)"
else
  _fail "benchmark_run processed $TOTAL tasks (expected >=3)"
fi

echo ""
echo "--- happy path: benchmark_results returns stored results ---"

RESULTS="$(benchmark_results "cand-001" 2>/dev/null)"
RES_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$RESULTS" 2>/dev/null || echo 0)"
if [[ "$RES_COUNT" -ge 1 ]]; then
  _ok "benchmark_results returns stored results for candidate"
else
  _fail "benchmark_results returned 0 results (expected >=1)"
fi

echo ""
echo "--- edge case: benchmark_run with empty task table returns 0 total_tasks ---"

# Fresh DB with no tasks — apply migrations so benchmark_run sees the
# canonical schema (not the _bench_ensure_tables fake one).
TEST_DB2=$(mktemp /tmp/mini-ork-test2-XXXXXX.db)
MINI_ORK_DB="$TEST_DB2"
test_apply_migrations >/dev/null
benchmark_add '{"id":"empty-seed","task_class":"dummy"}' >/dev/null 2>&1
# Delete it so table is empty
sqlite3 "$TEST_DB2" "DELETE FROM benchmark_tasks;" 2>/dev/null
RUN_EMPTY="$(benchmark_run "cand-empty" 2>/dev/null)"
TOTAL_EMPTY="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['total_tasks'])" "$RUN_EMPTY" 2>/dev/null || echo -1)"
_assert_eq "benchmark_run on empty task table returns total_tasks=0" "$TOTAL_EMPTY" "0"
rm -f "$TEST_DB2"
MINI_ORK_DB="$TEST_DB"

echo ""
echo "--- error path: benchmark_add with missing id and task_class exits non-zero ---"

if benchmark_add '{"input":"no-id-no-class"}' >/dev/null 2>&1; then
  _fail "benchmark_add with missing id/task_class should exit non-zero"
else
  _ok "benchmark_add with missing id/task_class exits non-zero"
fi

echo ""
echo "--- error path: benchmark_add with invalid JSON exits non-zero ---"

if benchmark_add "bad-json" >/dev/null 2>&1; then
  _fail "benchmark_add with invalid JSON should exit non-zero"
else
  _ok "benchmark_add with invalid JSON exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
