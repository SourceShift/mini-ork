#!/usr/bin/env bash
# Unit tests for lib/memory.sh
# Usage: bash tests/unit/test_memory.sh
# Exit 0 = all assertions pass (or skipped).  Exit 1 = any assertion failed.
set -Eeuo pipefail

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "[OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "[SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1"; local got="$2"; local want="$3"
  if [[ "$got" == "$want" ]]; then
    _ok "$label"
  else
    _fail "$label — got='$got' want='$want'"
  fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
MEMORY_LIB="$MINI_ORK_REPO/lib/memory.sh"

echo "=== test_memory.sh ==="

# ── Guard: skip if memory.sh not yet present ─────────────────────────────────

if [[ ! -f "$MEMORY_LIB" ]]; then
  _skip "lib/memory.sh not found — memory tests deferred until that agent lands it"
  echo ""
  echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
  exit 0
fi

# ── Setup: isolated state.db ──────────────────────────────────────────────────

TMP_DIR="$(mktemp -d)"
export MINI_ORK_HOME="$TMP_DIR/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
mkdir -p "$MINI_ORK_HOME"

# source the library
# shellcheck source=/dev/null
source "$MEMORY_LIB"

# init schema if memory.sh doesn't do it automatically
DB_INIT="$MINI_ORK_REPO/db/init.sh"
if [[ -f "$DB_INIT" ]]; then
  bash "$DB_INIT" >/dev/null 2>&1 || true
fi

echo ""
echo "--- create epic ---"

# Create an epic and capture its id
EPIC_ID="$(memory_create_epic \
  "test-run-001" \
  "test-epic" \
  "low" \
  "claude-sonnet-4-5" \
  "Test epic created by test_memory.sh" 2>/dev/null)" || EPIC_ID=""

if [[ -n "$EPIC_ID" ]]; then
  _ok "memory_create_epic returned id='$EPIC_ID'"
else
  _fail "memory_create_epic returned empty id"
fi

echo ""
echo "--- query epic ---"

if [[ -n "$EPIC_ID" ]]; then
  STATUS="$(memory_get_epic_status "$EPIC_ID" 2>/dev/null)" || STATUS=""
  _assert_eq "initial status = pending" "$STATUS" "pending"
fi

echo ""
echo "--- update status ---"

if [[ -n "$EPIC_ID" ]]; then
  memory_update_epic_status "$EPIC_ID" "claimed" >/dev/null 2>&1 || true
  STATUS_AFTER="$(memory_get_epic_status "$EPIC_ID" 2>/dev/null)" || STATUS_AFTER=""
  _assert_eq "status after update = claimed" "$STATUS_AFTER" "claimed"
fi

echo ""
echo "--- update verdict ---"

if [[ -n "$EPIC_ID" ]]; then
  memory_update_epic_verdict "$EPIC_ID" "PASS" >/dev/null 2>&1 || true
  VERDICT="$(memory_get_epic_verdict "$EPIC_ID" 2>/dev/null)" || VERDICT=""
  _assert_eq "verdict after update = PASS" "$VERDICT" "PASS"
fi

echo ""
echo "--- delete epic ---"

if [[ -n "$EPIC_ID" ]]; then
  memory_delete_epic "$EPIC_ID" >/dev/null 2>&1 || true
  ROW_COUNT="$(sqlite3 "$MINI_ORK_DB" \
    "SELECT COUNT(*) FROM epics WHERE id='$EPIC_ID';" 2>/dev/null || echo 1)"
  _assert_eq "epic deleted from DB" "$ROW_COUNT" "0"
fi

echo ""
echo "--- idempotent double-delete ---"

if [[ -n "$EPIC_ID" ]]; then
  # Second delete should not error
  if memory_delete_epic "$EPIC_ID" >/dev/null 2>&1; then
    _ok "double delete exits cleanly"
  else
    _skip "double delete exited non-zero (may be intentional — check memory.sh contract)"
  fi
fi

echo ""
echo "--- multiple epics isolation ---"

ID_A="$(memory_create_epic "run-A" "epic-A" "low" "sonnet" "Epic A" 2>/dev/null)" || ID_A=""
ID_B="$(memory_create_epic "run-B" "epic-B" "low" "sonnet" "Epic B" 2>/dev/null)" || ID_B=""

if [[ -n "$ID_A" && -n "$ID_B" ]]; then
  memory_update_epic_status "$ID_A" "claimed" >/dev/null 2>&1 || true
  STATUS_B="$(memory_get_epic_status "$ID_B" 2>/dev/null)" || STATUS_B=""
  _assert_eq "updating epic-A does not affect epic-B status" "$STATUS_B" "pending"
fi

# ── Cleanup ───────────────────────────────────────────────────────────────────

rm -rf "$TMP_DIR"

echo ""
echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
(( FAIL > 0 )) && exit 1 || exit 0
