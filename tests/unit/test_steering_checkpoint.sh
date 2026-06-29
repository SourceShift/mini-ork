#!/usr/bin/env bash
# tests/unit/test_steering_checkpoint.sh — unit tests for lib/steering_checkpoint.sh
#
# Verifies the HITL steering checkpoint gate:
#   - has_unconsumed: present/absent/expired/consumed/role-targeted rows
#   - gate: pause (rc=2 + marker) when no steering; proceed (rc=0 + cleared)
#     when a steering row is present
#   - mark / clear / status round-trip
#
# Usage: bash tests/unit/test_steering_checkpoint.sh
# Exit 0 = all assertions pass.  Exit 1 = any failed.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
# shellcheck source=/dev/null
. "$MINI_ORK_ROOT/lib/steering_checkpoint.sh"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

# ── fixture: throwaway home + state.db with operator_steering ──────────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export MINI_ORK_HOME="$TMP/.mini-ork"
mkdir -p "$MINI_ORK_HOME"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
unset MINI_ORK_RUN_DIR

python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.executescript("""
CREATE TABLE operator_steering (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    role_target TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    source TEXT,
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at INTEGER NOT NULL,
    consumed_at INTEGER,
    expires_at INTEGER NOT NULL
);
""")
con.commit(); con.close()
PY

NOW_MS="$(_mo_steering_now_ms)"
FUTURE=$((NOW_MS + 600000))
PAST=$((NOW_MS - 1000))

_insert() {  # run_id(or "NULL") role consumed_at(or "") expires_at
  python3 - "$MINI_ORK_DB" "$1" "$2" "$3" "$4" "$NOW_MS" <<'PY'
import sqlite3, sys
db, run_id, role, consumed, expires, now = sys.argv[1:7]
con = sqlite3.connect(db)
con.execute(
    "INSERT INTO operator_steering(run_id, role_target, message, created_at, consumed_at, expires_at) "
    "VALUES (?, ?, ?, ?, ?, ?)",
    (None if run_id == "NULL" else run_id, role, "msg", int(now),
     None if consumed == "" else int(consumed), int(expires)),
)
con.commit(); con.close()
PY
}

# 1. empty → no steering
mo_steering_has_unconsumed "run-a" && _fail "empty db reports steering" || _ok "empty db → no steering"

# 2. gate with no steering → pause (rc=2) + marker written
export MINI_ORK_RUN_DIR="$MINI_ORK_HOME/runs/run-a"; mkdir -p "$MINI_ORK_RUN_DIR"
mo_steering_checkpoint_gate "run-a" "scope_checkpoint"; rc=$?
[ "$rc" -eq 2 ] && _ok "gate pauses (rc=2) when no steering" || _fail "gate rc=$rc, want 2"
[ -f "$MINI_ORK_RUN_DIR/.steering-checkpoint" ] && _ok "sentinel written on pause" || _fail "no sentinel after pause"
echo "$(mo_steering_checkpoint_status run-a)" | grep -q '"awaiting": *true' && _ok "status reports awaiting" || _fail "status not awaiting"

# 3. add a steering row for run-a → has_unconsumed true, gate proceeds + clears
_insert "run-a" "any" "" "$FUTURE"
mo_steering_has_unconsumed "run-a" && _ok "detects unconsumed row" || _fail "missed unconsumed row"
mo_steering_checkpoint_gate "run-a" "scope_checkpoint"; rc=$?
[ "$rc" -eq 0 ] && _ok "gate proceeds (rc=0) when steering present" || _fail "gate rc=$rc, want 0"
[ -f "$MINI_ORK_RUN_DIR/.steering-checkpoint" ] && _fail "sentinel not cleared on proceed" || _ok "sentinel cleared on proceed"

# 4. expired row → not actionable
_insert "run-b" "any" "" "$PAST"
mo_steering_has_unconsumed "run-b" && _fail "expired row counted" || _ok "expired row ignored"

# 5. consumed row → not actionable
_insert "run-c" "any" "$NOW_MS" "$FUTURE"
mo_steering_has_unconsumed "run-c" && _fail "consumed row counted" || _ok "consumed row ignored"

# 6. role targeting: planner-only row not actionable for a reviewer query.
#    (Run this BEFORE the global-row test — a global 'any' row matches every
#    run/role by design and would mask role targeting.)
_insert "run-d" "planner" "" "$FUTURE"
mo_steering_has_unconsumed "run-d" "reviewer" && _fail "planner row matched reviewer" || _ok "role targeting respected"
mo_steering_has_unconsumed "run-d" "planner" && _ok "planner row matches planner" || _fail "planner row missed planner"

# 7. global NULL-run row is actionable for any run (asserted last — it matches all)
_insert "NULL" "any" "" "$FUTURE"
mo_steering_has_unconsumed "run-zzz" && _ok "global NULL-run steering applies to any run" || _fail "global steering missed"

echo ""
echo "steering_checkpoint: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
