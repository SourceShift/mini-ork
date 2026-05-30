#!/usr/bin/env bash
# tests/unit/test_gate_registry.sh — unit tests for lib/gate_registry.sh
# Usage: bash tests/unit/test_gate_registry.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/gate_registry.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: gate_registry.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/gate_registry.sh not found — tests deferred"
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
echo "--- happy path: gate_register returns a gate_id ---"

GID="$(gate_register "budget_gate" "10.0" "" 2>/dev/null)"
if [[ -n "$GID" ]]; then
  _ok "gate_register returns non-empty gate_id"
else
  _fail "gate_register returned empty id"
fi

echo ""
echo "--- happy path: gate_evaluate budget_gate passes when cost <= limit ---"

VERDICT="$(gate_evaluate "$GID" '{"cost_usd":5.0}' 2>/dev/null)"
_assert_eq "budget_gate passes when cost 5.0 <= limit 10.0" "$VERDICT" "pass"

echo ""
echo "--- happy path: gate_evaluate budget_gate fails when cost > limit ---"

VERDICT_FAIL="$(gate_evaluate "$GID" '{"cost_usd":15.0}' 2>/dev/null)"
_assert_eq "budget_gate fails when cost 15.0 > limit 10.0" "$VERDICT_FAIL" "fail"

echo ""
echo "--- happy path: gate_evaluate human_gate always defers ---"

HID="$(gate_register "human_gate" "human-review" "" 2>/dev/null)"
HUMAN_VERDICT="$(gate_evaluate "$HID" '{"task_class":"any"}' 2>/dev/null)"
_assert_eq "human_gate always defers" "$HUMAN_VERDICT" "defer"

echo ""
echo "--- happy path: gate_evaluate scope_gate passes for allowed task class ---"

SID="$(gate_register "scope_gate" '["write-code","review-code"]' "write-code" 2>/dev/null)"
SCOPE_PASS="$(gate_evaluate "$SID" '{"task_class":"write-code"}' 2>/dev/null)"
_assert_eq "scope_gate passes for allowed task_class" "$SCOPE_PASS" "pass"

SCOPE_FAIL="$(gate_evaluate "$SID" '{"task_class":"delete-everything"}' 2>/dev/null)"
_assert_eq "scope_gate fails for forbidden task_class" "$SCOPE_FAIL" "fail"

echo ""
echo "--- happy path: gate_list returns registered gates ---"

LIST="$(gate_list 2>/dev/null)"
LIST_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$LIST" 2>/dev/null || echo 0)"
if [[ "$LIST_COUNT" -ge 3 ]]; then
  _ok "gate_list returns at least 3 gates"
else
  _fail "gate_list returned $LIST_COUNT gates (expected >=3)"
fi

echo ""
echo "--- happy path: gate_run_all summarizes all applicable gates ---"

SUMMARY="$(gate_run_all "write-code" '{"task_class":"write-code","cost_usd":3.0}' 2>/dev/null)"
if python3 -c "import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if 'gate_count' in d else 1)" "$SUMMARY" 2>/dev/null; then
  _ok "gate_run_all returns JSON with gate_count field"
  GATE_COUNT="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['gate_count'])" "$SUMMARY" 2>/dev/null || echo 0)"
  if [[ "$GATE_COUNT" -ge 1 ]]; then
    _ok "gate_run_all evaluated $GATE_COUNT gates"
  else
    _fail "gate_run_all evaluated 0 gates"
  fi
else
  _fail "gate_run_all did not return valid JSON summary: $SUMMARY"
fi

echo ""
echo "--- edge case: gate_evaluate on non-existent gate_id returns fail ---"

MISSING_VERDICT="$(gate_evaluate "gate-doesnotexist" '{}' 2>/dev/null)"
_assert_eq "gate_evaluate on missing gate_id returns fail" "$MISSING_VERDICT" "fail"

echo ""
echo "--- error path: gate_register with invalid gate_type exits non-zero ---"

if gate_register "invalid_gate_type" "condition" "" >/dev/null 2>&1; then
  _fail "gate_register with invalid gate_type should exit non-zero"
else
  _ok "gate_register with invalid gate_type exits non-zero"
fi

echo ""
echo "--- error path: gate_register missing required args exits non-zero ---"

if bash -c "source '$LIB' 2>/dev/null; gate_register" >/dev/null 2>&1; then
  _fail "gate_register with no args should exit non-zero"
else
  _ok "gate_register with no args exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
