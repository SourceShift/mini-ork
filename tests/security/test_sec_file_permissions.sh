#!/usr/bin/env bash
# tests/security/test_sec_file_permissions.sh
#
# SECURITY TEST — File permission hardening after mini-ork init
#
# THREAT MODEL:
#   After init, sensitive files (state.db, secrets/) are world-readable or
#   world-writable.  A local attacker (another user on the same host) could read
#   task history, agent credentials, or overwrite the DB.
#
# EXPECTED BEHAVIOUR (hardened):
#   - state.db:       permissions <= 644 (owner+group read, no world write)
#   - secrets/ dir:   permissions <= 700 (owner-only; no group or world access)
#   - config/agents.yaml: <= 644 (readable, not world-writable)
#   - No file in .mini-ork/ is world-writable (mode bit o+w absent)
#
# KNOWN GAP (v0.1):
#   db/init.sh creates state.db with default umask permissions (typically 644
#   on macOS/Linux with umask 022, but 666 on some systems with umask 000).
#   It does NOT explicitly `chmod 600 "$DB"` after creation.
#   init also does NOT create a secrets/ dir with explicit `chmod 700`.
#   This test documents the gap and asserts the minimum safe configuration.
#
# VULNERABILITY SHAPE IF FAILING:
#   state.db is world-writable (o+w) — another user can corrupt task history.
#   secrets/ is world-readable (o+r) — another user can read agent credentials.

set -uo pipefail

PASS=0; FAIL=0; SKIP=0; WARN=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== test_sec_file_permissions.sh ==="
echo "    Testing: file permission hardening after mini-ork init"
echo ""

# ── Run init in an isolated tmp dir ──────────────────────────────────────────

TMPDIR_TEST="$(mktemp -d /tmp/mo-sec-perms-XXXXXX)"
export MINI_ORK_HOME="$TMPDIR_TEST/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

trap 'rm -rf "$TMPDIR_TEST"' EXIT

init_bin="$MINI_ORK_ROOT/bin/mini-ork-init"
db_init_sh="$MINI_ORK_ROOT/db/init.sh"

# Run init to create the structure
INIT_CMD=""
if [[ -x "$init_bin" ]]; then
  INIT_CMD="$init_bin"
else
  INIT_CMD="$db_init_sh"
fi

if [[ -z "$INIT_CMD" || ! -f "$INIT_CMD" ]]; then
  _skip "No init script found — permissions test skipped"
  echo ""
  echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL (WARN: $WARN) ==="
  exit 0
fi

# Ensure .mini-ork dir exists (init may need it or create it)
mkdir -p "$MINI_ORK_HOME"
INIT_EXIT=0
bash "$INIT_CMD" >/dev/null 2>&1 || INIT_EXIT=$?

if [[ "$INIT_EXIT" -ne 0 ]]; then
  echo "  [NOTE] init exited $INIT_EXIT — some files may not have been created; testing what exists"
fi
echo ""

# ── Helper: get octal permissions (portable macOS + Linux) ───────────────────
get_perms_octal() {
  local path="$1"
  # macOS stat: -f "%OLp" gives octal permissions (3 digits)
  # Linux stat:  --format="%a"
  stat -f "%OLp" "$path" 2>/dev/null \
    || stat --format="%a" "$path" 2>/dev/null \
    || echo "000"
}

# Helper: check if 'other' write bit is set (bit 1 of last octal digit)
has_other_write() {
  local perm_octal="$1"
  local last_digit="${perm_octal: -1}"
  # o+w = bit 1 in last digit: 2 (010), 3 (011), 6 (110), 7 (111)
  [[ "$last_digit" =~ [2367] ]]
}

# Helper: check if 'other' read bit is set (r=4 in octal permission digit)
# Last octal digit: 4=r--, 5=r-x, 6=rw-, 7=rwx
has_other_read() {
  local perm_octal="$1"
  local last_digit="${perm_octal: -1}"
  # o+r = the 4-bit is set in last digit: values 4,5,6,7
  [[ "$last_digit" =~ [4567] ]]
}

# Helper: numeric compare, accepting "at most" a given octal value
# perm_octal <= max_octal (compare as integer base-8)
perms_lte() {
  local actual="$1" max="$2"
  # Zero-pad both to 4 digits for comparison
  printf -v a '%04d' "$actual" 2>/dev/null || a="$actual"
  printf -v m '%04d' "$max" 2>/dev/null || m="$max"
  [[ "$((8#$a))" -le "$((8#$m))" ]]
}

# ── Test 1: state.db permissions <= 644 ──────────────────────────────────────
echo "--- 1. state.db permissions <= 644 ---"
DB_PATH="$MINI_ORK_DB"

if [[ ! -f "$DB_PATH" ]]; then
  _skip "state.db not created by init — permissions test skipped for DB"
else
  DB_PERMS="$(get_perms_octal "$DB_PATH")"
  echo "    state.db permissions: $DB_PERMS"

  if has_other_write "$DB_PERMS"; then
    _fail "state.db is WORLD-WRITABLE ($DB_PERMS) — another user can corrupt task history. FIX: chmod 600 \"\$DB\" in db/init.sh after creation"
  else
    _ok "state.db is NOT world-writable ($DB_PERMS)"
  fi

  # Ideal is 600 or 640 — warn on 644 (world-readable)
  if has_other_read "$DB_PERMS"; then
    _warn "state.db is world-readable ($DB_PERMS) — task history (kickoff paths, run IDs, verdicts) readable by any user. Ideal: chmod 600 in db/init.sh"
  else
    _ok "state.db is not world-readable ($DB_PERMS) — well hardened"
  fi
fi
echo ""

# ── Test 2: secrets/ directory is 700 (owner-only) ───────────────────────────
echo "--- 2. secrets/ directory is 700 (owner-only) ---"
SECRETS_DIR="$MINI_ORK_HOME/secrets"

if [[ ! -d "$SECRETS_DIR" ]]; then
  _skip "secrets/ dir not created by init — test skipped (should be created with chmod 700)"
else
  SEC_PERMS="$(get_perms_octal "$SECRETS_DIR")"
  echo "    secrets/ permissions: $SEC_PERMS"

  if has_other_read "$SEC_PERMS" || has_other_write "$SEC_PERMS"; then
    _fail "secrets/ is accessible by others ($SEC_PERMS) — agent credentials may leak. FIX: chmod 700 \"\$MINI_ORK_HOME/secrets\" in mini-ork-init"
  else
    _ok "secrets/ has no world access ($SEC_PERMS)"
  fi

  # Also check group bits (should not be readable by group in an ideal setup)
  SEC_GROUP_DIGIT="${SEC_PERMS: -2:1}"
  if [[ "$SEC_GROUP_DIGIT" =~ [4567] ]]; then
    _warn "secrets/ is group-readable ($SEC_PERMS) — if other users share your group this leaks credentials. Ideal: chmod 700"
  fi
fi
echo ""

# ── Test 3: config/agents.yaml <= 644 ────────────────────────────────────────
echo "--- 3. config/agents.yaml <= 644 ---"
AGENTS_YAML="$MINI_ORK_HOME/config/agents.yaml"

if [[ ! -f "$AGENTS_YAML" ]]; then
  _skip "config/agents.yaml not created — permissions test skipped"
else
  AGENTS_PERMS="$(get_perms_octal "$AGENTS_YAML")"
  echo "    agents.yaml permissions: $AGENTS_PERMS"

  if has_other_write "$AGENTS_PERMS"; then
    _fail "config/agents.yaml is WORLD-WRITABLE ($AGENTS_PERMS) — attacker can change model routing. FIX: chmod 644 on creation"
  else
    _ok "config/agents.yaml is not world-writable ($AGENTS_PERMS)"
  fi
fi
echo ""

# ── Test 4: No file in .mini-ork/ is world-writable ──────────────────────────
echo "--- 4. No file in .mini-ork/ is world-writable ---"

if [[ ! -d "$MINI_ORK_HOME" ]]; then
  _skip ".mini-ork/ not created — skipping world-writable scan"
else
  # Find world-writable files (o+w) — exclude symlinks
  WW_FILES=$(find "$MINI_ORK_HOME" -type f -perm /o+w 2>/dev/null)

  if [[ -z "$WW_FILES" ]]; then
    _ok "No world-writable files found in .mini-ork/"
  else
    while IFS= read -r wf; do
      _fail "World-writable file: $wf"
    done <<< "$WW_FILES"
  fi

  # Also check world-writable directories (except the root .mini-ork itself is typically 755)
  WW_DIRS=$(find "$MINI_ORK_HOME" -type d -perm /o+w 2>/dev/null)
  if [[ -z "$WW_DIRS" ]]; then
    _ok "No world-writable directories in .mini-ork/"
  else
    while IFS= read -r wd; do
      _fail "World-writable directory: $wd"
    done <<< "$WW_DIRS"
  fi
fi
echo ""

# ── Test 5: locks/ directory not world-accessible ────────────────────────────
echo "--- 5. locks/ directory not world-writable ---"
LOCKS_DIR="$MINI_ORK_HOME/locks"

if [[ ! -d "$LOCKS_DIR" ]]; then
  _skip "locks/ dir not created by init — test skipped"
else
  LOCKS_PERMS="$(get_perms_octal "$LOCKS_DIR")"
  if has_other_write "$LOCKS_PERMS"; then
    _fail "locks/ is world-writable ($LOCKS_PERMS) — attacker can create fake lock files to disrupt orchestration"
  else
    _ok "locks/ is not world-writable ($LOCKS_PERMS)"
  fi
fi
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL (WARN: $WARN) ==="
(( FAIL > 0 )) && exit 1 || exit 0
