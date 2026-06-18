#!/usr/bin/env bash
# tests/unit/test_grounded_rejection.sh — unit tests for
# lib/gates_common.sh (HarnessBridge Technique 4, arxiv:2606.12882).
#
# Covers:
#   - mo_grounded_rejection_tuple_json shape (concern/evidence/suggestion)
#   - mo_grounded_rejection emits a row with provenance
#   - invalid verdict rejected (rc=2)
#   - invalid evidence_trace_ids JSON rejected (rc=3)
#   - append-only trigger blocks UPDATE of provenance fields
#   - consumed_by_reflector_ts is updatable
#   - missing required args rejected (rc=2)
#   - default evidence_trace_ids = '[]' works

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/gates_common.sh"
MIGRATION="$MINI_ORK_ROOT/db/migrations/0037_grounded_rejection.sql"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: lib/gates_common.sh ──"

if [ ! -f "$LIB" ]; then
  _skip "lib/gates_common.sh not found"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi
if [ ! -f "$MIGRATION" ]; then
  _skip "migration 0037 not found"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TEST_HOME=$(mktemp -d)
export MINI_ORK_HOME="$TEST_HOME"
export MINI_ORK_DB="$TEST_HOME/state.db"
trap 'rm -rf "$TEST_HOME"' EXIT
sqlite3 "$MINI_ORK_DB" < "$MIGRATION"

# shellcheck disable=SC1090
source "$LIB"

# Case 1: tuple_json shape.
out=$(mo_grounded_rejection_tuple_json "stale plan" "trace tr-1 vs current schema" "re-derive plan after schema refresh")
if printf '%s' "$out" | python3 -c "import sys,json; t=json.load(sys.stdin); assert set(t.keys())=={'concern','evidence','suggestion'}; print('ok')" 2>/dev/null | grep -q ok; then
  _ok "tuple_json has concern + evidence + suggestion keys"
else
  _fail "tuple_json shape wrong"
fi

# Case 2: valid emit.
id=$(mo_grounded_rejection "coalition" "fail" \
     "panel approved by single family" \
     "tr-aaa, tr-bbb both reported by claude-sonnet family" \
     "fail the gate; require additional family from kimi or codex" \
     '["tr-aaa","tr-bbb"]' \
     "run-unit-1")
if [ -n "$id" ] && [ ${#id} -ge 16 ]; then
  _ok "emit returned id=$id"
else
  _fail "emit did not return valid id"
fi

# Case 3: invalid verdict.
set +e
mo_grounded_rejection "coalition" "TOTAL_FAIL" "x" "y" "z" '[]' >/dev/null 2>&1
rc=$?
set -e
[ "$rc" = "2" ] && _ok "invalid verdict rejected with rc=2" || _fail "verdict validation broken rc=$rc"

# Case 4: invalid JSON array.
set +e
mo_grounded_rejection "coalition" "fail" "x" "y" "z" 'not-json' >/dev/null 2>&1
rc=$?
set -e
[ "$rc" = "3" ] && _ok "non-array JSON rejected with rc=3" || _fail "JSON validation broken rc=$rc"

# Case 5: object instead of array also rejected.
set +e
mo_grounded_rejection "coalition" "fail" "x" "y" "z" '{"foo":"bar"}' >/dev/null 2>&1
rc=$?
set -e
[ "$rc" = "3" ] && _ok "non-array JSON (object) rejected with rc=3" || _fail "JSON array check broken rc=$rc"

# Case 6: missing required arg.
set +e
mo_grounded_rejection "coalition" "fail" "x" "y" "" >/dev/null 2>&1
rc=$?
set -e
[ "$rc" = "2" ] && _ok "missing suggestion rejected with rc=2" || _fail "arg validation broken rc=$rc"

# Case 7: append-only trigger blocks immutable column UPDATE.
set +e
err=$(sqlite3 "$MINI_ORK_DB" "UPDATE grounded_rejections SET concern='hacked' WHERE id='$id'" 2>&1)
rc=$?
set -e
if [ "$rc" != "0" ] && printf '%s' "$err" | grep -q immutable; then
  _ok "append-only trigger blocks concern UPDATE"
else
  _fail "append-only trigger leaked: rc=$rc err=$err"
fi

# Case 8: consumed_by_reflector_ts is updatable.
set +e
err=$(sqlite3 "$MINI_ORK_DB" "UPDATE grounded_rejections SET consumed_by_reflector_ts=strftime('%s','now') WHERE id='$id'" 2>&1)
rc=$?
set -e
if [ "$rc" = "0" ]; then
  ts=$(sqlite3 "$MINI_ORK_DB" "SELECT consumed_by_reflector_ts FROM grounded_rejections WHERE id='$id'")
  [ -n "$ts" ] && _ok "consumed_by_reflector_ts UPDATE allowed and set ($ts)" || _fail "consumed_ts not set"
else
  _fail "consumed_ts UPDATE blocked: $err"
fi

# Case 9: DELETE is blocked.
set +e
err=$(sqlite3 "$MINI_ORK_DB" "DELETE FROM grounded_rejections WHERE id='$id'" 2>&1)
rc=$?
set -e
if [ "$rc" != "0" ] && printf '%s' "$err" | grep -q append-only; then
  _ok "append-only trigger blocks DELETE"
else
  _fail "DELETE trigger leaked: rc=$rc"
fi

# Case 10: default evidence_trace_ids = '[]' works.
id2=$(mo_grounded_rejection "citation_verifier" "needs_revision" \
      "3 invalid citations" \
      "report_path /tmp/cv.tsv" \
      "add file:LINE anchors to RSP.md TW section")
if [ -n "$id2" ] && [ ${#id2} -ge 16 ]; then
  _ok "default evidence_trace_ids=[] works"
else
  _fail "default evidence_trace_ids broken"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" = "0" ] || exit 1
exit 0
