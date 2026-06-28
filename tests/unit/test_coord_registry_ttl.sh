#!/usr/bin/env bash
# tests/unit/test_coord_registry_ttl.sh — unit tests for lib/coord_registry.sh
# time-bounded lease behavior (Track B3).
#
# Verifies:
#   - Default TTL is 120s when ttl_seconds is omitted
#   - Max TTL is 3600s (1 hour); values above are capped silently
#   - A lease whose expires_at has passed is treated as free on read
#     (self-heal: crashed holder claim heals on next acquire)
#   - coord_renew extends a live lease's expires_at when called by holder
#   - coord_renew rejects a non-holder (rc=3)
#   - coord_renew rejects an unknown / expired lease id (rc=1)
#   - coord_renew uses default TTL when ttl_seconds is omitted
#   - bin/mini-ork-coord renew subcommand wires through to coord_renew
#
# Usage: bash tests/unit/test_coord_registry_ttl.sh
# Exit 0 = all assertions pass. Exit 1 = any assertion failed.

set -uo pipefail

# Resolve MINI_ORK_ROOT from this script's location, ignoring any inherited
# env value (the harness exports MINI_ORK_ROOT pointing at the main checkout,
# which would mis-resolve when the test is run inside an implementer worktree).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/coord_registry.sh"
WRAPPER="$MINI_ORK_ROOT/bin/mini-ork-coord"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

_reset_state() {
  : >"$TEST_STATE"
}

# _expire_all: rewrite every lease's expires_at to a past timestamp so the
# next coord_acquire/coord_release/coord_renew treats them as expired.
_expire_all() {
  python3 - <<PY
import json, os, time
path = os.environ["COORD_REGISTRY_STATE_FILE"]
if not os.path.exists(path):
    raise SystemExit(0)
data = json.load(open(path))
for rec in data.get("leases", {}).values():
    rec["expires_at"] = int(time.time()) - 1000
open(path, "w").write(json.dumps(data))
PY
}

# _lease_expires_at <lease_id>  → emits the numeric expires_at (or empty)
_lease_expires_at() {
  local lid="$1"
  python3 - "$lid" <<PY
import json, os, sys
path = os.environ["COORD_REGISTRY_STATE_FILE"]
data = json.load(open(path))
rec = data.get("leases", {}).get("$lid")
if rec is None:
    sys.exit(0)
print(int(rec.get("expires_at", 0)))
PY
}

echo "── unit: coord_registry.sh TTL (Track B3) ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/coord_registry.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TEST_DIR=$(mktemp -d)
TEST_STATE="$TEST_DIR/leases.json"
export COORD_REGISTRY_STATE_FILE="$TEST_STATE"
trap 'rm -rf "$TEST_DIR"' EXIT

# shellcheck source=/dev/null
source "$LIB"

# ─── 1. Default TTL is 120s when ttl_seconds omitted ───────────────────
echo ""
echo "--- default TTL = 120s when ttl omitted ---"
_reset_state
ID=$(coord_acquire agent-a src/api write 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "acquire with omitted ttl should succeed (uses default)"
else
  EXPIRES_AT="$(_lease_expires_at "$ID")"
  NOW=$(python3 -c 'import time; print(int(time.time()))')
  DELTA=$((EXPIRES_AT - NOW))
  if [ "$DELTA" -ge 115 ] && [ "$DELTA" -le 125 ]; then
    _ok "default ttl ~120s (expires_at-now=$DELTA)"
  else
    _fail "default ttl out of range (expires_at-now=$DELTA, expected 115..125)"
  fi
fi

# ─── 2. Default TTL also applies to coord_renew ────────────────────────
echo ""
echo "--- default TTL = 120s on coord_renew ---"
_reset_state
ID=$(coord_acquire agent-a src/api write 60 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "setup acquire failed"
else
  if coord_renew agent-a "$ID" 2>/dev/null; then
    EXPIRES_AT="$(_lease_expires_at "$ID")"
    NOW=$(python3 -c 'import time; print(int(time.time()))')
    DELTA=$((EXPIRES_AT - NOW))
    if [ "$DELTA" -ge 115 ] && [ "$DELTA" -le 125 ]; then
      _ok "renew default ttl ~120s (delta=$DELTA)"
    else
      _fail "renew default ttl out of range (delta=$DELTA)"
    fi
  else
    _fail "renew with omitted ttl should succeed"
  fi
fi

# ─── 3. Max TTL is 3600s; larger values are capped silently ────────────
echo ""
echo "--- max TTL = 3600s; values above cap silently ---"
_reset_state
ID=$(coord_acquire agent-a src/api write 99999 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "acquire with ttl=99999 should succeed (capped)"
else
  EXPIRES_AT="$(_lease_expires_at "$ID")"
  NOW=$(python3 -c 'import time; print(int(time.time()))')
  DELTA=$((EXPIRES_AT - NOW))
  if [ "$DELTA" -ge 3595 ] && [ "$DELTA" -le 3605 ]; then
    _ok "ttl capped at 3600s (delta=$DELTA)"
  else
    _fail "ttl not capped as expected (delta=$DELTA)"
  fi
fi
# And renew also caps.
_reset_state
ID=$(coord_acquire agent-a src/api write 60 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "setup acquire failed"
else
  if coord_renew agent-a "$ID" 99999 2>/dev/null; then
    EXPIRES_AT="$(_lease_expires_at "$ID")"
    NOW=$(python3 -c 'import time; print(int(time.time()))')
    DELTA=$((EXPIRES_AT - NOW))
    if [ "$DELTA" -ge 3595 ] && [ "$DELTA" -le 3605 ]; then
      _ok "renew ttl capped at 3600s (delta=$DELTA)"
    else
      _fail "renew ttl not capped as expected (delta=$DELTA)"
    fi
  else
    _fail "renew with ttl=99999 should succeed (capped)"
  fi
fi

# ─── 4. Lease auto-frees after TTL (self-heal) ─────────────────────────
echo ""
echo "--- lease auto-frees after TTL: crashed holder self-heals ---"
_reset_state
ID=$(coord_acquire agent-crashed src/api write 60 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "setup acquire for crashed holder failed"
else
  # Simulate the holder dying and the clock passing its TTL.
  _expire_all
  # A competing writer on the same path should now succeed (claim is free).
  if NEW_ID=$(coord_acquire agent-rescue src/api write 30 2>/dev/null) \
       && [[ -n "$NEW_ID" ]] && [[ "$NEW_ID" =~ ^[A-Fa-f0-9]+$ ]]; then
    _ok "competing writer succeeded after holder's lease expired (new lease_id=$NEW_ID)"
    # And the rescue writer's lease must NOT include the crashed one.
    if [ "$NEW_ID" != "$ID" ]; then
      _ok "new lease has a fresh lease_id (crashed lease pruned)"
    else
      _fail "new lease reused crashed lease id"
    fi
    # The expired lease should be gone from state.
    if python3 - "$ID" <<PY; then
import json, os, sys
path = os.environ["COORD_REGISTRY_STATE_FILE"]
data = json.load(open(path))
sys.exit(0 if "$ID" not in data.get("leases", {}) else 1)
PY
      _ok "expired lease pruned from state on read"
    else
      _fail "expired lease still present in state"
    fi
  else
    _fail "competing writer should succeed after crashed holder's lease expired"
  fi
fi

# ─── 5. coord_renew extends a live lease for the holder ────────────────
echo ""
echo "--- coord_renew extends a live lease for the holder ---"
_reset_state
ID=$(coord_acquire agent-holder src/api write 60 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "setup acquire failed"
else
  EXPIRES_BEFORE="$(_lease_expires_at "$ID")"
  # Renew with a clearly larger window (well under max cap).
  if coord_renew agent-holder "$ID" 600 2>/dev/null; then
    EXPIRES_AFTER="$(_lease_expires_at "$ID")"
    if [ "$EXPIRES_AFTER" -gt "$EXPIRES_BEFORE" ]; then
      DELTA=$((EXPIRES_AFTER - EXPIRES_BEFORE))
      if [ "$DELTA" -ge 500 ] && [ "$DELTA" -le 700 ]; then
        _ok "renew extended expires_at by ~600s (delta=$DELTA)"
      else
        _fail "renew extended by unexpected delta=$DELTA"
      fi
    else
      _fail "renew did not advance expires_at (before=$EXPIRES_BEFORE after=$EXPIRES_AFTER)"
    fi
  else
    _fail "renew by holder should succeed"
  fi
fi

# ─── 6. coord_renew rejects non-holder (rc=3) ──────────────────────────
echo ""
echo "--- coord_renew rejects non-holder (rc=3) ---"
_reset_state
ID=$(coord_acquire agent-holder src/api write 60 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "setup acquire failed"
else
  set +e
  coord_renew agent-other "$ID" 60 2>/dev/null
  RC=$?
  set -e
  if [ "$RC" -eq 3 ]; then
    _ok "renew by non-holder rejected (rc=3)"
  else
    _fail "renew by non-holder returned rc=$RC (expected 3)"
  fi
  # Lease must NOT be mutated by a rejected renew.
  EXPIRES_NOW="$(_lease_expires_at "$ID")"
  NOW=$(python3 -c 'import time; print(int(time.time()))')
  ORIGINAL_DELTA=$((EXPIRES_NOW - NOW))
  if [ "$ORIGINAL_DELTA" -ge 55 ] && [ "$ORIGINAL_DELTA" -le 65 ]; then
    _ok "rejected renew did not mutate lease (delta still ~60s)"
  else
    _fail "rejected renew mutated lease (delta=$ORIGINAL_DELTA)"
  fi
fi

# ─── 7. coord_renew rejects expired / unknown lease id (rc=1) ──────────
echo ""
echo "--- coord_renew rejects expired / unknown lease id (rc=1) ---"
_reset_state
ID=$(coord_acquire agent-holder src/api write 60 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "setup acquire failed"
else
  # Expire it then renew.
  _expire_all
  set +e
  coord_renew agent-holder "$ID" 60 2>/dev/null
  RC=$?
  set -e
  if [ "$RC" -eq 1 ]; then
    _ok "renew on expired lease rejected (rc=1)"
  else
    _fail "renew on expired lease returned rc=$RC (expected 1)"
  fi
  # And unknown id likewise.
  set +e
  coord_renew agent-holder deadbeef 60 2>/dev/null
  RC=$?
  set -e
  if [ "$RC" -eq 1 ]; then
    _ok "renew on unknown id rejected (rc=1)"
  else
    _fail "renew on unknown id returned rc=$RC (expected 1)"
  fi
fi

# ─── 8. coord_renew arg validation (rc=2) ──────────────────────────────
echo ""
echo "--- coord_renew arg validation (rc=2) ---"
_reset_state
set +e
coord_renew "" "" 60 2>/dev/null; RC=$?
if [ "$RC" -eq 2 ]; then _ok "missing args → rc=2"; else _fail "missing args rc=$RC (expected 2)"; fi
coord_renew agent-x "" 60 2>/dev/null; RC=$?
if [ "$RC" -eq 2 ]; then _ok "missing lease_id → rc=2"; else _fail "missing lease_id rc=$RC (expected 2)"; fi
coord_renew agent-x abcdef -1 2>/dev/null; RC=$?
if [ "$RC" -eq 2 ]; then _ok "negative ttl → rc=2"; else _fail "negative ttl rc=$RC (expected 2)"; fi
coord_renew agent-x abcdef abc 2>/dev/null; RC=$?
if [ "$RC" -eq 2 ]; then _ok "non-numeric ttl → rc=2"; else _fail "non-numeric ttl rc=$RC (expected 2)"; fi
set -e

# ─── 9. bin/mini-ork-coord renew subcommand wires through ──────────────
echo ""
echo "--- bin/mini-ork-coord renew subcommand wires through ---"
if [[ ! -x "$WRAPPER" ]]; then
  _skip "bin/mini-ork-coord not executable — wrapper test deferred"
else
  _reset_state
  ID=$("$WRAPPER" acquire agent-w src/api write 60 2>/dev/null) || true
  if [ -z "$ID" ]; then
    _fail "wrapper acquire failed"
  else
    if "$WRAPPER" renew agent-w "$ID" 600 2>/dev/null; then
      _ok "wrapper renew succeeded"
    else
      _fail "wrapper renew failed"
    fi
    if "$WRAPPER" renew agent-other "$ID" 60 2>/dev/null; then
      _fail "wrapper renew by non-holder should fail"
    else
      _ok "wrapper renew by non-holder rejected"
    fi
  fi
fi

# ─── 10. Expired lease releases clean (rc=1 on release) ────────────────
echo ""
echo "--- expired lease release returns non-zero ---"
_reset_state
ID=$(coord_acquire agent-a src/api write 60 2>/dev/null) || true
if [ -z "$ID" ]; then
  _fail "setup acquire failed"
else
  _expire_all
  set +e
  coord_release "$ID" 2>/dev/null
  RC=$?
  set -e
  if [ "$RC" -ne 0 ]; then
    _ok "release of expired lease returned rc=$RC (no-op success)"
  else
    _fail "release of expired lease should return non-zero"
  fi
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1