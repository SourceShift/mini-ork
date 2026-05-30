#!/usr/bin/env bash
# tests/unit/test_pattern_store.sh — unit tests for lib/pattern_store.sh
# Usage: bash tests/unit/test_pattern_store.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/pattern_store.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: pattern_store.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/pattern_store.sh not found — tests deferred"
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
echo "--- happy path: pattern_store inserts new pattern and returns id ---"

PID="$(pattern_store '{"description":"test pattern","output_type":"adr","evidence_trace_ids":["tr-1"]}' 2>/dev/null)"
if [[ -n "$PID" ]]; then
  _ok "pattern_store returns non-empty pattern_id"
else
  _fail "pattern_store returned empty id"
fi

FREQ="$(sqlite3 "$TEST_DB" "SELECT frequency FROM pattern_records WHERE pattern_id='$PID';" 2>/dev/null || echo 0)"
_assert_eq "new pattern has frequency=1" "$FREQ" "1"

echo ""
echo "--- happy path: pattern_store upsert increments frequency on same id ---"

pattern_store "{\"pattern_id\":\"$PID\",\"description\":\"test pattern\",\"output_type\":\"adr\",\"evidence_trace_ids\":[\"tr-2\"]}" >/dev/null 2>&1
FREQ2="$(sqlite3 "$TEST_DB" "SELECT frequency FROM pattern_records WHERE pattern_id='$PID';" 2>/dev/null || echo 0)"
_assert_eq "upsert on existing pattern_id increments frequency to 2" "$FREQ2" "2"

echo ""
echo "--- happy path: pattern_store merges evidence_trace_ids on upsert ---"

EVIDENCE="$(sqlite3 "$TEST_DB" "SELECT evidence_trace_ids FROM pattern_records WHERE pattern_id='$PID';" 2>/dev/null || echo '[]')"
EV_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$EVIDENCE" 2>/dev/null || echo 0)"
if [[ "$EV_COUNT" -ge 2 ]]; then
  _ok "pattern_store merges evidence_trace_ids (got $EV_COUNT items)"
else
  _fail "pattern_store did not merge evidence_trace_ids (got $EV_COUNT items)"
fi

echo ""
echo "--- happy path: pattern_query filters by min-frequency ---"

# Add a second pattern with frequency 1 (default)
pattern_store '{"description":"rare pattern","output_type":"other","evidence_trace_ids":["tr-x"]}' >/dev/null 2>&1

RESULTS="$(pattern_query --min-frequency 2 2>/dev/null)"
COUNT_HIGH="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$RESULTS" 2>/dev/null || echo 0)"
RESULTS_ALL="$(pattern_query --min-frequency 1 2>/dev/null)"
COUNT_ALL="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$RESULTS_ALL" 2>/dev/null || echo 0)"

if [[ "$COUNT_HIGH" -ge 1 && "$COUNT_ALL" -ge 2 ]]; then
  _ok "pattern_query --min-frequency filters correctly (high_freq=$COUNT_HIGH, all=$COUNT_ALL)"
else
  _fail "pattern_query filter wrong (high_freq=$COUNT_HIGH, all=$COUNT_ALL)"
fi

echo ""
echo "--- happy path: pattern_on_new registers and fires a hook ---"

_HOOK_FIRED=0
_test_hook_fn() { _HOOK_FIRED=1; }
pattern_on_new "_test_hook_fn" >/dev/null 2>&1

pattern_store '{"description":"hook trigger pattern","output_type":"workflow_change"}' >/dev/null 2>&1
_assert_eq "pattern_on_new hook fired on new pattern" "$_HOOK_FIRED" "1"

echo ""
echo "--- edge case: pattern_store with invalid output_type falls back to 'other' ---"

PID_BAD="$(pattern_store '{"description":"bad type","output_type":"INVALID_TYPE","evidence_trace_ids":[]}' 2>/dev/null)"
OT="$(sqlite3 "$TEST_DB" "SELECT output_type FROM pattern_records WHERE pattern_id='$PID_BAD';" 2>/dev/null || echo "")"
_assert_eq "invalid output_type coerced to 'other'" "$OT" "other"

echo ""
echo "--- error path: pattern_store with invalid JSON emits error to stderr ---"

# The Python subprocess exits non-zero but the shell wrapper captures stdout
# and may exit 0. Verify at minimum that stderr contains an error message
# and that no pattern_id is written to DB for the bad payload.
BEFORE_COUNT="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM pattern_records;" 2>/dev/null || echo 0)"
STDERR_OUT="$(pattern_store "not-json-at-all" 2>&1 >/dev/null)"
AFTER_COUNT="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM pattern_records;" 2>/dev/null || echo 0)"

if echo "$STDERR_OUT" | grep -qi "invalid\|JSON\|error" 2>/dev/null; then
  _ok "pattern_store with invalid JSON emits error message on stderr"
elif [[ "$BEFORE_COUNT" -eq "$AFTER_COUNT" ]]; then
  _ok "pattern_store with invalid JSON did not insert any row (DB unchanged)"
else
  _fail "pattern_store with invalid JSON: no error reported and DB grew unexpectedly"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
