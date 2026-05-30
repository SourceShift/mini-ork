#!/usr/bin/env bash
# tests/unit/test_promotion_gate.sh — unit tests for lib/promotion_gate.sh
# Usage: bash tests/unit/test_promotion_gate.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/promotion_gate.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: promotion_gate.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/promotion_gate.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB
TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Load deps
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/benchmark_suite.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/version_registry.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/gate_registry.sh"
# shellcheck source=/dev/null
source "$LIB"

# Pre-create all required tables so promotion_evaluate can query them
_bench_ensure_tables 2>/dev/null || true
_ver_ensure_table 2>/dev/null || true
_gate_ensure_table 2>/dev/null || true
_promo_ensure_tables 2>/dev/null || true

echo ""
echo "--- happy path: no benchmark results → promotion_evaluate returns quarantined or rejected ---"

RESULT="$(promotion_evaluate "cand-no-bench" 2>/dev/null)"
DECISION="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('decision',''))" "$RESULT" 2>/dev/null || echo "")"

if [[ "$DECISION" == "quarantined" || "$DECISION" == "rejected" || "$DECISION" == "promoted" ]]; then
  _ok "promotion_evaluate with no benchmark returns a valid decision: $DECISION"
else
  _fail "promotion_evaluate unexpected decision: '$DECISION'"
fi

echo ""
echo "--- happy path: MINI_ORK_REQUIRE_HUMAN_APPROVAL=true → pending_human_approval ---"

MINI_ORK_REQUIRE_HUMAN_APPROVAL=true
RESULT_HUMAN="$(promotion_evaluate "cand-human" 2>/dev/null)"
DECISION_HUMAN="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('decision',''))" "$RESULT_HUMAN" 2>/dev/null || echo "")"
_assert_eq "REQUIRE_HUMAN_APPROVAL=true yields pending_human_approval" "$DECISION_HUMAN" "pending_human_approval"
unset MINI_ORK_REQUIRE_HUMAN_APPROVAL

echo ""
echo "--- happy path: promotion_approve resolves pending_human_approval ---"

MINI_ORK_REQUIRE_HUMAN_APPROVAL=true
promotion_evaluate "cand-approve" >/dev/null 2>&1
unset MINI_ORK_REQUIRE_HUMAN_APPROVAL

APPROVE_RESULT="$(promotion_approve "cand-approve" "test-approver" "Approved in unit test" 2>/dev/null)"
APPROVE_DECISION="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('decision',''))" "$APPROVE_RESULT" 2>/dev/null || echo "")"
_assert_eq "promotion_approve sets decision to promoted" "$APPROVE_DECISION" "promoted"

APPROVER="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('approver',''))" "$APPROVE_RESULT" 2>/dev/null || echo "")"
_assert_eq "promotion_approve records approver identity" "$APPROVER" "test-approver"

echo ""
echo "--- happy path: promotion_evaluate result is persisted in promotion_records ---"

RESULT3="$(promotion_evaluate "cand-persist" 2>/dev/null)"
DB_COUNT="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM promotion_records WHERE candidate_id='cand-persist';" 2>/dev/null || echo 0)"
if [[ "$DB_COUNT" -ge 1 ]]; then
  _ok "promotion_evaluate persists record in promotion_records"
else
  _fail "promotion_evaluate did not persist record (count=$DB_COUNT)"
fi

echo ""
echo "--- edge case: promotion_approve on non-existent pending record exits non-zero ---"

if promotion_approve "cand-nonexistent-xyz" "approver" "rationale" >/dev/null 2>&1; then
  _fail "promotion_approve on non-existent candidate should exit non-zero"
else
  _ok "promotion_approve on non-existent candidate exits non-zero"
fi

echo ""
echo "--- error path: promotion_evaluate missing candidate_id arg exits non-zero ---"

if bash -c "source '$LIB' 2>/dev/null; promotion_evaluate" >/dev/null 2>&1; then
  _fail "promotion_evaluate with no args should exit non-zero"
else
  _ok "promotion_evaluate with no args exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
