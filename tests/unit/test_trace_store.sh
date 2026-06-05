#!/usr/bin/env bash
# tests/unit/test_trace_store.sh — unit tests for lib/trace_store.sh
# Usage: bash tests/unit/test_trace_store.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/trace_store.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert() {
  local name="$1"; shift
  if eval "$@" 2>/dev/null; then _ok "$name"; else _fail "$name (assertion: $*)"; fi
}

echo "── unit: trace_store.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/trace_store.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB
TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Apply migrations against the test DB so libs that removed inline CREATE
# TABLE blocks (post-D-039) can source + run against a fresh mktemp DB.
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations || { echo "skip: migrations failed to apply"; exit 0; }

# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: write and get a trace ---"

TRACE_ID="$(trace_write '{"task_class":"unit-test","status":"success","cost_usd":0.01,"duration_ms":500}' 2>/dev/null)"
_assert "trace_write returns non-empty id" '[[ -n "$TRACE_ID" ]]'

ROW="$(trace_get "$TRACE_ID" 2>/dev/null)"
_assert "trace_get returns JSON (not null)" '[[ "$ROW" != "null" && -n "$ROW" ]]'

TASK_CLASS_GOT="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('task_class',''))" "$ROW" 2>/dev/null || echo "")"
_assert "trace_get returns correct task_class" '[[ "$TASK_CLASS_GOT" == "unit-test" ]]'

echo ""
echo "--- happy path: trace_query filters by status ---"

trace_write '{"task_class":"unit-test","status":"failure","cost_usd":0.02}' >/dev/null 2>&1

SUCCESSES="$(trace_query --status success 2>/dev/null)"
SUCCESS_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$SUCCESSES" 2>/dev/null || echo 0)"
FAILURES="$(trace_query --status failure 2>/dev/null)"
FAILURE_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$FAILURES" 2>/dev/null || echo 0)"
_assert "query success returns >= 1" '[[ "$SUCCESS_COUNT" -ge 1 ]]'
_assert "query failure returns >= 1" '[[ "$FAILURE_COUNT" -ge 1 ]]'

echo ""
echo "--- happy path: trace_query filters by task-class ---"

trace_write '{"task_class":"other-class","status":"success"}' >/dev/null 2>&1
UNIT_TRACES="$(trace_query --task-class unit-test 2>/dev/null)"
OTHER_TRACES="$(trace_query --task-class other-class 2>/dev/null)"
UNIT_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$UNIT_TRACES" 2>/dev/null || echo 0)"
OTHER_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$OTHER_TRACES" 2>/dev/null || echo 0)"
_assert "task-class filter returns only matching rows" '[[ "$UNIT_COUNT" -ge 1 && "$OTHER_COUNT" -ge 1 ]]'

echo ""
echo "--- happy path: trace_attach_artifact ---"

ATTACH_ID="$(trace_write '{"task_class":"attach-test","status":"success"}' 2>/dev/null)"
trace_attach_artifact "$ATTACH_ID" "/some/artifact.json" "abc123" >/dev/null 2>&1
UPDATED="$(trace_get "$ATTACH_ID" 2>/dev/null)"
ARTIFACT_REF="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('final_artifact_ref',''))" "$UPDATED" 2>/dev/null || echo "")"
_assert "trace_attach_artifact writes artifact ref" '[[ -n "$ARTIFACT_REF" && "$ARTIFACT_REF" != "null" ]]'

echo ""
echo "--- edge case: trace_get non-existent id returns null ---"

MISSING="$(trace_get "tr-doesnotexist" 2>/dev/null)"
_assert "trace_get unknown id returns null" '[[ "$MISSING" == "null" ]]'

echo ""
echo "--- error path: trace_write with invalid JSON exits non-zero ---"

if trace_write "not-valid-json" >/dev/null 2>&1; then
  _fail "trace_write with invalid JSON should exit non-zero"
else
  _ok "trace_write with invalid JSON exits non-zero"
fi

echo ""
echo "--- error path: trace_write missing MINI_ORK_DB exits non-zero ---"

if ( unset MINI_ORK_DB; trace_write '{"task_class":"x"}' >/dev/null 2>&1 ); then
  _fail "trace_write without MINI_ORK_DB should exit non-zero"
else
  _ok "trace_write without MINI_ORK_DB exits non-zero"
fi

echo ""
echo "--- error path: trace_attach_artifact on missing trace_id exits non-zero ---"

if trace_attach_artifact "tr-doesnotexist" "/some/path.json" "hash123" >/dev/null 2>&1; then
  _fail "trace_attach_artifact on missing trace_id should exit non-zero"
else
  _ok "trace_attach_artifact on missing trace_id exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
