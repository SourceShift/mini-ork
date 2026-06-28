#!/usr/bin/env bash
# tests/unit/test_coord_registry.sh — unit tests for lib/coord_registry.sh
#
# Verifies prefix-aware many-readers-or-one-writer conflict semantics:
#   - write+write on overlapping prefixes   → BLOCK
#   - read +write on overlapping prefixes   → BLOCK (and W+R too)
#   - read +read  on overlapping prefixes   → ALLOW
#   - any mode on non-overlapping prefixes  → ALLOW
#   - prefix boundary: src/api overlaps src/api/x.rs but NOT src/ap
#   - mode must be exactly "read" or "write" (unknown → reject)
#
# Usage: bash tests/unit/test_coord_registry.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/coord_registry.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

# _assert_acquire_ok <label> <agent> <path> <mode> <ttl>  → emits lease_id on stdout
_assert_acquire_ok() {
  local label="$1" agent="$2" path="$3" mode="$4" ttl="$5"
  local id
  if id=$(coord_acquire "$agent" "$path" "$mode" "$ttl" 2>/dev/null) \
       && [[ -n "$id" ]] && [[ "$id" =~ ^[A-Fa-f0-9]+$ ]]; then
    _ok "$label (lease_id=$id)"
    LAST_LEASE_ID="$id"
  else
    _fail "$label — coord_acquire failed but should have succeeded"
    LAST_LEASE_ID=""
  fi
}

_assert_acquire_blocked() {
  local label="$1" agent="$2" path="$3" mode="$4" ttl="$5"
  local id rc
  set +e
  id=$(coord_acquire "$agent" "$path" "$mode" "$ttl" 2>/dev/null)
  rc=$?
  set -e
  if [ "$rc" -ne 0 ] && [ -z "$id" ]; then
    _ok "$label (blocked as expected, rc=$rc)"
  else
    _fail "$label — coord_acquire should have failed (rc=$rc id=$id)"
  fi
}

_assert_acquire_rejected() {
  local label="$1" agent="$2" path="$3" mode="$4" ttl="$5" expected_rc="$6"
  local id rc
  set +e
  id=$(coord_acquire "$agent" "$path" "$mode" "$ttl" 2>/dev/null)
  rc=$?
  set -e
  if [ "$rc" -eq "$expected_rc" ]; then
    _ok "$label (rejected rc=$rc)"
  else
    _fail "$label — expected rc=$expected_rc, got rc=$rc (id=$id)"
  fi
}

_assert_acquire_deadlock_abort() {
  local label="$1" agent="$2" path="$3" mode="$4" ttl="$5"
  local out rc
  set +e
  out=$(coord_acquire "$agent" "$path" "$mode" "$ttl" 2>/dev/null)
  rc=$?
  set -e
  if [ "$rc" -ne 4 ]; then
    _fail "$label — expected deadlock abort rc=4, got rc=$rc (out=$out)"
    return
  fi
  if python3 - "$out" "$agent" <<'PY'; then
import json
import sys

payload = json.loads(sys.argv[1])
agent = sys.argv[2]
assert payload["status"] == "abort"
assert payload["reason"] == "deadlock"
assert payload["agent"] == agent
assert agent in payload["cycle"]
PY
    _ok "$label (deadlock abort payload=$out)"
  else
    _fail "$label — malformed deadlock abort payload: $out"
  fi
}

_reset_state() {
  COORD_REGISTRY_STATE_FILE="$TEST_STATE" coord_release "" 2>/dev/null || true
  : >"$TEST_STATE"
}

_lease_count() {
  python3 - <<'PY'
import json
import os

path = os.environ["COORD_REGISTRY_STATE_FILE"]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {"leases": {}}
print(len(data.get("leases", {})))
PY
}

_waits_for() {
  local agent="$1"
  python3 - "$agent" <<'PY'
import json
import os
import sys

agent = sys.argv[1]
path = os.environ["COORD_REGISTRY_STATE_FILE"]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {"waits": {}}
print(",".join(data.get("waits", {}).get(agent, [])))
PY
}

echo "── unit: coord_registry.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/coord_registry.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated state file (per-test process; coord_registry uses flock)
TEST_DIR=$(mktemp -d)
TEST_STATE="$TEST_DIR/leases.json"
TEST_LOCK="$TEST_STATE.lock"
export COORD_REGISTRY_STATE_FILE="$TEST_STATE"
trap 'rm -rf "$TEST_DIR"' EXIT

# shellcheck source=/dev/null
source "$LIB"

LAST_LEASE_ID=""

# ─── 1. write+write on overlapping prefix → BLOCK ────────────────────────
echo ""
echo "--- W+W blocks overlapping prefix ---"
_reset_state
_assert_acquire_ok    "writer1 acquires src/api"          "agent-w1" "src/api"      "write" "60"
_assert_acquire_blocked "writer2 blocked on src/api/x.rs"  "agent-w2" "src/api/x.rs" "write" "60"
_assert_acquire_blocked "writer3 blocked on src/api"      "agent-w3" "src/api"      "write" "60"
_reset_state

# ─── 2. read+write on overlapping prefix → BLOCK (both directions) ───────
echo ""
echo "--- R+W blocks overlapping prefix (both directions) ---"
_reset_state
_assert_acquire_ok    "reader1 acquires src/api"          "agent-r1" "src/api"      "read"  "60"
_assert_acquire_blocked "writer2 blocked on src/api/x.rs"  "agent-w2" "src/api/x.rs" "write" "60"
_reset_state
_assert_acquire_ok    "writer1 acquires src/api"          "agent-w1" "src/api"      "write" "60"
_assert_acquire_blocked "reader2 blocked on src/api/x.rs"  "agent-r2" "src/api/x.rs" "read"  "60"
_reset_state
_assert_acquire_ok    "writer1 acquires src/api"          "agent-w1" "src/api"      "write" "60"
_assert_acquire_blocked "reader2 blocked on src/api (exact)" "agent-r2" "src/api"   "read"  "60"
_reset_state

# ─── 3. read+read on overlapping prefix → ALLOW ──────────────────────────
echo ""
echo "--- R+R permits overlapping prefix ---"
_reset_state
_assert_acquire_ok "reader1 acquires src/api"          "agent-r1" "src/api"      "read" "60"
_assert_acquire_ok "reader2 also acquires src/api/x.rs" "agent-r2" "src/api/x.rs" "read" "60"
_assert_acquire_ok "reader3 also acquires src/api"     "agent-r3" "src/api"      "read" "60"
_reset_state

# ─── 4. any mode on non-overlapping prefixes → ALLOW ─────────────────────
echo ""
echo "--- non-overlapping prefixes allowed (read+read disjoint) ---"
_reset_state
_assert_acquire_ok "reader1 acquires src/api" "agent-r1" "src/api" "read"  "60"
_assert_acquire_ok "reader2 acquires src/db"  "agent-r2" "src/db"  "read"  "60"
_assert_acquire_ok "writer1 acquires src/cli" "agent-w1" "src/cli" "write" "60"
_reset_state

# ─── 5. prefix-boundary scoping: src/api overlaps src/api/x.rs but NOT src/ap
echo ""
echo "--- prefix boundary: src/api overlaps src/api/x.rs but NOT src/ap ---"
_reset_state
_assert_acquire_ok "writer1 acquires src/api"          "agent-w1" "src/api"     "write" "60"
_assert_acquire_blocked "writer2 blocked on src/api/x.rs (overlap)" "agent-w2" "src/api/x.rs" "write" "60"
_assert_acquire_ok    "reader3 allowed on src/ap (no overlap)"      "agent-r3" "src/ap"       "read"  "60"
coord_release "$LAST_LEASE_ID" 2>/dev/null || _fail "reader3 release before writer4 boundary check failed"
_assert_acquire_ok    "writer4 allowed on src/ap (no overlap)"      "agent-w4" "src/ap"       "write" "60"
_reset_state

# ─── 6. mode validation: unknown modes are rejected with rc=2 ────────────
echo ""
echo "--- mode validation ---"
_reset_state
_assert_acquire_rejected "mode='weird' rejected"      "agent-x" "src/api" "weird" "60" 2
_assert_acquire_rejected "mode='' rejected"           "agent-x" "src/api" ""      "60" 2
_assert_acquire_rejected "ttl=0 rejected"             "agent-x" "src/api" "read"  "0"  2
_assert_acquire_rejected "ttl=-1 rejected"            "agent-x" "src/api" "write" "-1" 2
_assert_acquire_rejected "ttl='abc' rejected"         "agent-x" "src/api" "read"  "abc" 2
_reset_state

# ─── 7. release path works for valid leases, rejects unknown ─────────────
echo ""
echo "--- coord_release round-trip ---"
_reset_state
_assert_acquire_ok "reader1 acquires src/api" "agent-r1" "src/api" "read" "60"
RID="$LAST_LEASE_ID"
if [ -n "$RID" ] && COORD_REGISTRY_STATE_FILE="$TEST_STATE" coord_release "$RID" 2>/dev/null; then
  _ok "release valid lease succeeds"
else
  _fail "release valid lease failed"
fi
if COORD_REGISTRY_STATE_FILE="$TEST_STATE" coord_release "$RID" 2>/dev/null; then
  _fail "double-release should fail"
else
  _ok "double-release returns non-zero"
fi
if COORD_REGISTRY_STATE_FILE="$TEST_STATE" coord_release "nothex" 2>/dev/null; then
  _fail "malformed release should fail"
else
  _ok "malformed lease_id rejected"
fi
_reset_state

# ─── 8. wait-for graph aborts lower-priority requester on constructed cycle
echo ""
echo "--- wait-for graph: constructed cycle aborts lower-priority requester ---"
_reset_state
export COORD_REGISTRY_AGENT_PRIORITIES="agent-a=20,agent-b=10"
_assert_acquire_ok "agent-a holds src/a" "agent-a" "src/a" "write" "60"
_assert_acquire_ok "agent-b holds src/b" "agent-b" "src/b" "write" "60"
_assert_acquire_blocked "agent-a waits for agent-b" "agent-a" "src/b" "write" "60"
if [ "$(_waits_for agent-a)" = "agent-b" ]; then
  _ok "wait-for edge recorded: agent-a -> agent-b"
else
  _fail "wait-for edge missing for agent-a (got '$(_waits_for agent-a)')"
fi
_assert_acquire_deadlock_abort "agent-b completing cycle aborts lower-priority requester" "agent-b" "src/a" "write" "60"
if [ "$(_waits_for agent-b)" = "" ]; then
  _ok "aborted requester wait edge was removed"
else
  _fail "aborted requester wait edge still present (got '$(_waits_for agent-b)')"
fi
if [ "$(_lease_count)" = "2" ]; then
  _ok "active holders were not preempted by deadlock abort"
else
  _fail "active holder leases changed during deadlock abort (lease_count=$(_lease_count))"
fi
unset COORD_REGISTRY_AGENT_PRIORITIES
_reset_state

# ─── 9. higher-priority requester never preempts an active lower-priority holder
echo ""
echo "--- wait-for graph: higher-priority requester does not preempt holder ---"
_reset_state
export COORD_REGISTRY_AGENT_PRIORITIES="agent-a=10,agent-b=20"
_assert_acquire_ok "agent-a holds src/a" "agent-a" "src/a" "write" "60"
_assert_acquire_ok "agent-b holds src/b" "agent-b" "src/b" "write" "60"
_assert_acquire_blocked "agent-a waits for agent-b" "agent-a" "src/b" "write" "60"
_assert_acquire_blocked "agent-b cycle request blocks rather than preempting holder" "agent-b" "src/a" "write" "60"
if [ "$(_lease_count)" = "2" ]; then
  _ok "active lower-priority holder was not preempted"
else
  _fail "holder lease count changed for higher-priority requester (lease_count=$(_lease_count))"
fi
unset COORD_REGISTRY_AGENT_PRIORITIES
_reset_state

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
