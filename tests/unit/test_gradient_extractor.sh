#!/usr/bin/env bash
# tests/unit/test_gradient_extractor.sh — unit tests for lib/gradient_extractor.sh
# Usage: bash tests/unit/test_gradient_extractor.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/gradient_extractor.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: gradient_extractor.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/gradient_extractor.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB
TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Apply migrations so trace_store/gradient_extractor find execution_traces +
# gradient tables on first call.
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations || { echo "skip: migrations failed to apply"; exit 0; }

# Stub out LLM dispatch — gradient_extractor falls back to override fn when set
_test_gradient_extractor_stub() {
  local _trace_id="$1"
  # Emit one well-formed gradient JSON
  echo '{"target":"workflow.node.test","signal":"test signal","suggested_change":"test change","confidence":0.9}'
}
export MINI_ORK_GRADIENT_EXTRACTOR_FN="_test_gradient_extractor_stub"

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/trace_store.sh"
# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: gradient_store round-trips a gradient record ---"

GID="$(gradient_store '{"target":"workflow.node.planner","signal":"slow","suggested_change":"add cache","evidence":"tr-abc","confidence":0.8}' 2>/dev/null)"
_ok_cond() { [[ -n "$GID" ]] && echo "  [OK]   $1" && PASS=$((PASS+1)) || { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }; }
_ok_cond "gradient_store returns non-empty id"

# Verify the row is present in DB
ROW_COUNT="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records WHERE gradient_id='$GID';" 2>/dev/null || echo 0)"
if [[ "$ROW_COUNT" -eq 1 ]]; then _ok "gradient_store writes to DB"; else _fail "gradient_store did not write to DB (count=$ROW_COUNT)"; fi

echo ""
echo "--- happy path: gradient_extract via override fn ---"

# Create a trace to extract from
TRACE_ID="$(trace_write '{"task_class":"grad-test","status":"success"}' 2>/dev/null)"

EXTRACTED="$(gradient_extract "$TRACE_ID" 2>/dev/null)"
if [[ -n "$EXTRACTED" ]]; then
  _ok "gradient_extract (stub) emits at least one gradient line"
  TARGET="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('target',''))" "$EXTRACTED" 2>/dev/null || echo "")"
  if [[ "$TARGET" == "workflow.node.test" ]]; then
    _ok "gradient_extract stub output has expected target"
  else
    _fail "gradient_extract stub output wrong target: '$TARGET'"
  fi
else
  _fail "gradient_extract (stub) returned empty output"
fi

echo ""
echo "--- edge case: gradient_store upsert on same gradient_id increments confidence ---"

GID2="gr-dedupetest"
gradient_store "{\"gradient_id\":\"$GID2\",\"target\":\"wf.node.A\",\"signal\":\"signal1\",\"suggested_change\":\"change1\",\"evidence\":\"tr-x\",\"confidence\":0.3}" >/dev/null 2>&1
gradient_store "{\"gradient_id\":\"$GID2\",\"target\":\"wf.node.A\",\"signal\":\"signal1\",\"suggested_change\":\"change1 updated\",\"evidence\":\"tr-x\",\"confidence\":0.7}" >/dev/null 2>&1

CONF="$(sqlite3 "$TEST_DB" "SELECT confidence FROM gradient_records WHERE gradient_id='$GID2';" 2>/dev/null || echo 0)"
if python3 -c "import sys; sys.exit(0 if abs(float(sys.argv[1]) - 0.7) < 0.001 else 1)" "$CONF" 2>/dev/null; then
  _ok "gradient_store upsert updates confidence to latest value"
else
  _fail "gradient_store upsert confidence wrong: $CONF (expected 0.7)"
fi

echo ""
echo "--- error path: gradient_store with missing required field exits non-zero ---"

if gradient_store '{"target":"wf.node.X","signal":"s"}' >/dev/null 2>&1; then
  _fail "gradient_store with missing evidence/suggested_change should exit non-zero"
else
  _ok "gradient_store with missing required field exits non-zero"
fi

echo ""
echo "--- error path: gradient_store with invalid JSON exits non-zero ---"

if gradient_store "not-json" >/dev/null 2>&1; then
  _fail "gradient_store with invalid JSON should exit non-zero"
else
  _ok "gradient_store with invalid JSON exits non-zero"
fi

echo ""
echo "--- error path: gradient_extract on non-existent trace_id exits non-zero ---"

# Unset override so we test actual trace lookup failure path
unset MINI_ORK_GRADIENT_EXTRACTOR_FN
if gradient_extract "tr-doesnotexist" >/dev/null 2>&1; then
  _fail "gradient_extract on missing trace_id should exit non-zero"
else
  _ok "gradient_extract on missing trace_id exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
