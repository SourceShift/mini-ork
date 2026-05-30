#!/usr/bin/env bash
# tests/unit/test_agent_registry.sh — unit tests for lib/agent_registry.sh
# Usage: bash tests/unit/test_agent_registry.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/agent_registry.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: agent_registry.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/agent_registry.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB
TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: agent_register inserts a new version and returns version_id ---"

VID="$(agent_register "planner" '{"model":"claude-sonnet-4-5","provider":"anthropic","tools":["read","write"],"task_classes":["code-review"]}' 2>/dev/null)"
if [[ -n "$VID" ]]; then
  _ok "agent_register returns non-empty version_id"
else
  _fail "agent_register returned empty version_id"
fi

echo ""
echo "--- happy path: agent_get retrieves the registered version ---"

ROW="$(agent_get "planner" "$VID" 2>/dev/null)"
if [[ "$ROW" == "null" || -z "$ROW" ]]; then
  _fail "agent_get returned null for existing version_id $VID"
else
  MODEL="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('model',''))" "$ROW" 2>/dev/null || echo "")"
  _assert_eq "agent_get returns correct model" "$MODEL" "claude-sonnet-4-5"
fi

echo ""
echo "--- happy path: agent_current returns the active version ---"

CURRENT="$(agent_current "planner" 2>/dev/null)"
if [[ "$CURRENT" == "null" || -z "$CURRENT" ]]; then
  _fail "agent_current returned null"
else
  STATUS="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('status',''))" "$CURRENT" 2>/dev/null || echo "")"
  _assert_eq "agent_current returns status=active" "$STATUS" "active"
fi

echo ""
echo "--- happy path: registering a second version retires the first ---"

VID2="$(agent_register "planner" '{"model":"claude-opus-4-5","tools":["read"]}' 2>/dev/null)"

# First version should now be retired
STATUS_OLD="$(sqlite3 "$TEST_DB" "SELECT status FROM agent_registry WHERE version_id='$VID';" 2>/dev/null || echo "")"
_assert_eq "old version retired when new version registered" "$STATUS_OLD" "retired"

# New version should be active
STATUS_NEW="$(sqlite3 "$TEST_DB" "SELECT status FROM agent_registry WHERE version_id='$VID2';" 2>/dev/null || echo "")"
_assert_eq "new version has status=active" "$STATUS_NEW" "active"

echo ""
echo "--- happy path: agent_performance returns stats for all versions of role ---"

# Add a trace referencing the active agent version
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/trace_store.sh"
trace_write "{\"task_class\":\"code-review\",\"status\":\"success\",\"agent_version_id\":\"$VID2\",\"cost_usd\":0.03}" >/dev/null 2>&1

PERF="$(agent_performance "planner" 2>/dev/null)"
if python3 -c "import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if 'versions' in d and d['version_count']>=1 else 1)" "$PERF" 2>/dev/null; then
  _ok "agent_performance returns JSON with versions array"
  VER_COUNT="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['version_count'])" "$PERF" 2>/dev/null || echo 0)"
  if [[ "$VER_COUNT" -ge 2 ]]; then
    _ok "agent_performance shows $VER_COUNT versions for planner role"
  else
    _fail "agent_performance version_count=$VER_COUNT (expected >=2)"
  fi
else
  _fail "agent_performance did not return valid JSON: $PERF"
fi

echo ""
echo "--- edge case: agent_get on unknown version_id returns null ---"

MISSING="$(agent_get "planner" "av-doesnotexist" 2>/dev/null)"
_assert_eq "agent_get unknown version_id returns null" "$MISSING" "null"

echo ""
echo "--- edge case: agent_current for unknown role returns null ---"

NONE="$(agent_current "role-no-such-xyz" 2>/dev/null)"
_assert_eq "agent_current for unknown role returns null" "$NONE" "null"

echo ""
echo "--- error path: agent_register without required 'model' field exits non-zero ---"

if agent_register "executor" '{"provider":"anthropic"}' >/dev/null 2>&1; then
  _fail "agent_register without model should exit non-zero"
else
  _ok "agent_register without model exits non-zero"
fi

echo ""
echo "--- error path: agent_register with invalid JSON exits non-zero ---"

if agent_register "executor" "not-json" >/dev/null 2>&1; then
  _fail "agent_register with invalid JSON should exit non-zero"
else
  _ok "agent_register with invalid JSON exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
