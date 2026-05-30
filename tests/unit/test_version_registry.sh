#!/usr/bin/env bash
# tests/unit/test_version_registry.sh — unit tests for lib/version_registry.sh
# Usage: bash tests/unit/test_version_registry.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/version_registry.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: version_registry.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/version_registry.sh not found — tests deferred"
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
echo "--- happy path: version_register returns a version_id ---"

VID="$(version_register "workflow" '{"name":"wf-alpha","version":"1.0","status":"candidate"}' 2>/dev/null)"
if [[ -n "$VID" ]]; then
  _ok "version_register returns non-empty version_id"
else
  _fail "version_register returned empty id"
fi

echo ""
echo "--- happy path: version_get returns correct record ---"

ROW="$(version_get "workflow" "$VID" 2>/dev/null)"
if [[ "$ROW" == "null" || -z "$ROW" ]]; then
  _fail "version_get returned null for existing version_id $VID"
else
  NAME="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); p=json.loads(d.get('payload','{}')); print(p.get('name',''))" "$ROW" 2>/dev/null || echo "")"
  _assert_eq "version_get returns record with correct name" "$NAME" "wf-alpha"
fi

echo ""
echo "--- happy path: version_current returns stable version ---"

# Register a stable version
VID_STABLE="$(version_register "workflow" '{"name":"wf-beta","version":"2.0","status":"stable"}' 2>/dev/null)"
# Force status to stable in DB
sqlite3 "$TEST_DB" "UPDATE version_registry SET status='stable', promoted_at=strftime('%s','now') WHERE version_id='$VID_STABLE';" 2>/dev/null

CURRENT="$(version_current "workflow" "wf-beta" 2>/dev/null)"
if [[ "$CURRENT" != "null" && -n "$CURRENT" ]]; then
  _ok "version_current returns stable version for wf-beta"
else
  _fail "version_current returned null (expected stable version)"
fi

echo ""
echo "--- happy path: version_quarantine and version_can_promote ---"

VID_Q="$(version_register "workflow" '{"name":"wf-gamma","version":"3.0","status":"candidate"}' 2>/dev/null)"
version_quarantine "workflow" "$VID_Q" "test quarantine reason" >/dev/null 2>&1

CAN="$(version_can_promote "workflow" "$VID_Q" 2>/dev/null)"
_assert_eq "quarantined version cannot be promoted" "$CAN" "false"

echo ""
echo "--- happy path: version_clear_quarantine re-enables promotion ---"

version_clear_quarantine "$VID_Q" "test-approver" >/dev/null 2>&1
CAN_AFTER="$(version_can_promote "workflow" "$VID_Q" 2>/dev/null)"
_assert_eq "cleared quarantine version can be promoted" "$CAN_AFTER" "true"

echo ""
echo "--- happy path: version_rollback restores previous stable ---"

# Register two stable versions with previous linkage
VID_OLD="$(version_register "agent" '{"name":"ag-delta","version":"1.0","status":"stable"}' 2>/dev/null)"
sqlite3 "$TEST_DB" "UPDATE version_registry SET status='stable', promoted_at=strftime('%s','now')-10 WHERE version_id='$VID_OLD';" 2>/dev/null

VID_NEW="$(version_register "agent" '{"name":"ag-delta","version":"2.0","status":"stable"}' 2>/dev/null)"
# Manually set previous_stable_version to simulate the chain
sqlite3 "$TEST_DB" "UPDATE version_registry SET status='stable', promoted_at=strftime('%s','now'), previous_stable_version='$VID_OLD' WHERE version_id='$VID_NEW';" 2>/dev/null

ROLLED_BACK="$(version_rollback "agent" "ag-delta" 2>/dev/null)"
if [[ "$ROLLED_BACK" != "null" && -n "$ROLLED_BACK" ]]; then
  RB_VID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('version_id',''))" "$ROLLED_BACK" 2>/dev/null || echo "")"
  _assert_eq "version_rollback returned previous stable version_id" "$RB_VID" "$VID_OLD"
else
  _fail "version_rollback returned null"
fi

echo ""
echo "--- edge case: version_get on non-existent version returns null ---"

MISSING="$(version_get "workflow" "v-doesnotexist" 2>/dev/null)"
_assert_eq "version_get unknown id returns null" "$MISSING" "null"

echo ""
echo "--- edge case: version_current when no stable version returns null ---"

NONE="$(version_current "workflow" "wf-no-stable-xyz" 2>/dev/null)"
_assert_eq "version_current with no stable version returns null" "$NONE" "null"

echo ""
echo "--- error path: version_register with missing name exits non-zero ---"

if version_register "workflow" '{"version":"1.0"}' >/dev/null 2>&1; then
  _fail "version_register without name should exit non-zero"
else
  _ok "version_register without name exits non-zero"
fi

echo ""
echo "--- error path: version_rollback with no stable version exits non-zero ---"

if version_rollback "workflow" "wf-never-existed-xyz" >/dev/null 2>&1; then
  _fail "version_rollback with no stable version should exit non-zero"
else
  _ok "version_rollback with no stable version exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
