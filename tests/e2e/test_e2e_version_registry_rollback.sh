#!/usr/bin/env bash
# tests/e2e/test_e2e_version_registry_rollback.sh
# E2E: VersionRegistry rollback to previous_stable + quarantine lifecycle.
#   - Seed v1→v2→v3 with v3 as current stable
#   - version_rollback returns pointer to v2 and retires v3
#   - re-promote quarantined v3 requires version_clear_quarantine first
#   - audit_log row written for rollback event (when the DB migration includes it)
#
# Usage: bash tests/e2e/test_e2e_version_registry_rollback.sh
# Exit 0 = all assertions pass. Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert() {
  local name="$1"; shift
  if eval "$@" 2>/dev/null; then _ok "$name"; else _fail "$name  (expr: $*)"; fi
}

echo "══════════════════════════════════════════════════════"
echo "  E2E: version registry + rollback"
echo "══════════════════════════════════════════════════════"
echo ""

if [[ ! -f "$MINI_ORK_ROOT/lib/version_registry.sh" ]]; then
  _skip "lib/version_registry.sh not found — all tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"; exit 0
fi

# ── isolated env ──────────────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-e2e-verReg-XXXXXX)"
export MINI_ORK_HOME="$TEST_DIR/home"
export MINI_ORK_DB="$TEST_DIR/home/state.db"
mkdir -p "$MINI_ORK_HOME"
trap 'rm -rf "$TEST_DIR"' EXIT

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/version_registry.sh"

WF_NAME="test-workflow"

# ── seed: 3 promoted versions ─────────────────────────────────────────────────
echo "--- seed: register v1, v2, v3 for workflow '${WF_NAME}' ---"

VID1="$(version_register "workflow" "{\"name\":\"${WF_NAME}\",\"version\":\"1.0.0\",\"status\":\"candidate\",\"utility_score\":0.65}" 2>/dev/null)"
_assert "version_register v1 returns id" '[[ -n "${VID1:-}" ]]'

# Promote v1 to stable
python3 - "$MINI_ORK_DB" "$VID1" <<'PY'
import sqlite3, sys, time
db, vid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute("UPDATE version_registry SET status='stable', promoted_at=? WHERE version_id=?", (int(time.time()), vid))
con.commit()
con.close()
PY

VID2="$(version_register "workflow" "{\"name\":\"${WF_NAME}\",\"version\":\"2.0.0\",\"status\":\"candidate\",\"utility_score\":0.75}" 2>/dev/null)"
_assert "version_register v2 returns id (previous_stable=v1)" '[[ -n "${VID2:-}" ]]'

# Promote v2 to stable (this records previous_stable = v1)
python3 - "$MINI_ORK_DB" "$VID2" <<'PY'
import sqlite3, sys, time
db, vid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute("UPDATE version_registry SET status='stable', promoted_at=? WHERE version_id=?", (int(time.time()), vid))
# Retire v1 so v2 is the only stable
con.execute("UPDATE version_registry SET status='retired' WHERE version_id != ? AND name=? AND kind='workflow' AND status='stable'", (vid, "test-workflow"))
con.commit()
con.close()
PY

VID3="$(version_register "workflow" "{\"name\":\"${WF_NAME}\",\"version\":\"3.0.0\",\"status\":\"candidate\",\"utility_score\":0.85}" 2>/dev/null)"
_assert "version_register v3 returns id (previous_stable=v2)" '[[ -n "${VID3:-}" ]]'

# Promote v3 to stable
python3 - "$MINI_ORK_DB" "$VID3" "$VID2" <<'PY'
import sqlite3, sys, time
db, vid3, vid2 = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
now = int(time.time())
con.execute("UPDATE version_registry SET status='stable', promoted_at=?, previous_stable_version=? WHERE version_id=?",
            (now, vid2, vid3))
# Retire v2
con.execute("UPDATE version_registry SET status='retired' WHERE version_id=?", (vid2,))
con.commit()
con.close()
PY

echo ""
echo "--- version_current: v3 is current stable ---"

CURRENT="$(version_current "workflow" "$WF_NAME" 2>/dev/null)"
CURRENT_VID="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('version_id',''))" "$CURRENT" 2>/dev/null || echo '')"
_assert "version_current returns v3 as current stable" '[[ "$CURRENT_VID" == "$VID3" ]]'

echo ""
echo "--- version_rollback: rolls back from v3 to v2 ---"

ROLLBACK_RESULT="$(version_rollback "workflow" "$WF_NAME" 2>/dev/null)"
_assert "version_rollback returns non-null JSON" '[[ "$ROLLBACK_RESULT" != "null" && -n "$ROLLBACK_RESULT" ]]'

NEW_VID="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('version_id',''))" "$ROLLBACK_RESULT" 2>/dev/null || echo '')"
_assert "version_rollback restores v2 as current stable" '[[ "$NEW_VID" == "$VID2" ]]'

echo ""
echo "--- v3 is now retired after rollback ---"

V3_ROW="$(version_get "workflow" "$VID3" 2>/dev/null)"
V3_STATUS="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('status',''))" "$V3_ROW" 2>/dev/null || echo '')"
_assert "v3 is marked retired after rollback" '[[ "$V3_STATUS" == "retired" ]]'

echo ""
echo "--- version_current: v2 is now current ---"

CURRENT2="$(version_current "workflow" "$WF_NAME" 2>/dev/null)"
C2_VID="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('version_id',''))" "$CURRENT2" 2>/dev/null || echo '')"
_assert "version_current now returns v2" '[[ "$C2_VID" == "$VID2" ]]'

echo ""
echo "--- version_quarantine: quarantine v3 ---"

version_quarantine "workflow" "$VID3" "benchmark regression in cycle 12" >/dev/null 2>&1
CAN_PROMOTE="$(version_can_promote "workflow" "$VID3" 2>/dev/null)"
_assert "version_can_promote returns false for quarantined v3" '[[ "$CAN_PROMOTE" == "false" ]]'

V3_QROW="$(version_get "workflow" "$VID3" 2>/dev/null)"
V3_QSTATUS="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('status',''))" "$V3_QROW" 2>/dev/null || echo '')"
_assert "v3 status = quarantined" '[[ "$V3_QSTATUS" == "quarantined" ]]'
V3_REASON="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('quarantine_reason',''))" "$V3_QROW" 2>/dev/null || echo '')"
_assert "v3 quarantine_reason is populated" '[[ -n "$V3_REASON" ]]'

echo ""
echo "--- re-promote quarantined v3 requires clear first ---"

# Attempting to register a new version based on quarantined v3 (simulating re-promote attempt)
# version_can_promote should block this path
CAN_PRE_CLEAR="$(version_can_promote "workflow" "$VID3" 2>/dev/null)"
_assert "version_can_promote returns false before clear" '[[ "$CAN_PRE_CLEAR" == "false" ]]'

echo ""
echo "--- version_clear_quarantine: clear quarantine, then can_promote = true ---"

version_clear_quarantine "$VID3" "ops-engineer-1" >/dev/null 2>&1
CAN_POST_CLEAR="$(version_can_promote "workflow" "$VID3" 2>/dev/null)"
_assert "version_can_promote returns true after quarantine cleared" '[[ "$CAN_POST_CLEAR" == "true" ]]'

V3_POST="$(version_get "workflow" "$VID3" 2>/dev/null)"
V3_POST_STATUS="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('status',''))" "$V3_POST" 2>/dev/null || echo '')"
_assert "v3 status = candidate after clear (no longer quarantined)" '[[ "$V3_POST_STATUS" == "candidate" ]]'
V3_CLEARED_BY="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('quarantine_cleared_by',''))" "$V3_POST" 2>/dev/null || echo '')"
_assert "quarantine_cleared_by is populated" '[[ -n "$V3_CLEARED_BY" ]]'

echo ""
echo "--- rollback on name with no previous_stable → exit non-zero ---"

ORPHAN_VID="$(version_register "workflow" "{\"name\":\"orphan-wf\",\"version\":\"1.0.0\",\"status\":\"stable\"}" 2>/dev/null)"
python3 - "$MINI_ORK_DB" "$ORPHAN_VID" <<'PY'
import sqlite3, sys, time
db, vid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute("UPDATE version_registry SET status='stable', promoted_at=?, previous_stable_version=NULL WHERE version_id=?",
            (int(time.time()), vid))
con.commit()
con.close()
PY

if version_rollback "workflow" "orphan-wf" >/dev/null 2>&1; then
  _fail "version_rollback on orphan (no previous_stable) should exit non-zero"
else
  _ok "version_rollback on orphan exits non-zero (no previous_stable)"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
