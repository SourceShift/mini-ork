#!/usr/bin/env bash
# tests/integration/test_update_subcommand.sh - integration tests for mini-ork update
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"

TMPROOT=$(mktemp -d /tmp/ork-update-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

PASS=0
FAIL=0
_ok() { echo "  [OK]   $*"; PASS=$((PASS + 1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

_mtime() {
  if stat -f %m "$1" >/dev/null 2>&1; then
    stat -f %m "$1"
  else
    stat -c %Y "$1"
  fi
}

echo "-- integration: mini-ork update --"

echo ""
echo "--- 1. help exits 0 and prints usage ---"
HELP_OUT=$(mini-ork update --help 2>&1)
HELP_RC=$?
if [ "$HELP_RC" -eq 0 ] && echo "$HELP_OUT" | grep -qi "Usage: mini-ork update"; then
  _ok "update --help exits 0 and prints usage"
else
  _fail "update --help failed: rc=$HELP_RC out=$HELP_OUT"
fi

echo ""
echo "--- 2. dry-run lists pending work and does not write ---"
DRY_PROJECT="$TMPROOT/dry-project"
mkdir -p "$DRY_PROJECT/.mini-ork"
touch "$DRY_PROJECT/.mini-ork/state.db"
BEFORE_MTIME=$(_mtime "$DRY_PROJECT/.mini-ork/state.db")
sleep 1
(
  cd "$DRY_PROJECT" || exit 1
  export MINI_ORK_HOME="$DRY_PROJECT/.mini-ork"
  export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
  mini-ork update --dry-run
) > "$TMPROOT/dry.out" 2>&1
DRY_RC=$?
AFTER_MTIME=$(_mtime "$DRY_PROJECT/.mini-ork/state.db")
if [ "$DRY_RC" -eq 0 ] && grep -q "pending migration" "$TMPROOT/dry.out"; then
  _ok "dry-run lists pending migrations"
else
  _fail "dry-run did not list pending migrations: rc=$DRY_RC out=$(cat "$TMPROOT/dry.out")"
fi
if [ "$BEFORE_MTIME" = "$AFTER_MTIME" ] && [ ! -d "$DRY_PROJECT/.mini-ork/config" ]; then
  _ok "dry-run does not modify state.db or write config"
else
  _fail "dry-run modified files unexpectedly"
fi

echo ""
echo "--- 3. update applies migrations and is idempotent ---"
MIG_PROJECT="$TMPROOT/migration-project"
mkdir -p "$MIG_PROJECT/.mini-ork"
(
  cd "$MIG_PROJECT" || exit 1
  export MINI_ORK_HOME="$MIG_PROJECT/.mini-ork"
  export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
  mini-ork update
) > "$TMPROOT/update1.out" 2>&1
UPDATE1_RC=$?
COUNT1=$(sqlite3 "$MIG_PROJECT/.mini-ork/state.db" "SELECT COUNT(*) FROM schema_migrations;" 2>/dev/null || echo 0)
(
  cd "$MIG_PROJECT" || exit 1
  export MINI_ORK_HOME="$MIG_PROJECT/.mini-ork"
  export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
  mini-ork update
) > "$TMPROOT/update2.out" 2>&1
UPDATE2_RC=$?
COUNT2=$(sqlite3 "$MIG_PROJECT/.mini-ork/state.db" "SELECT COUNT(*) FROM schema_migrations;" 2>/dev/null || echo 0)
if [ "$UPDATE1_RC" -eq 0 ] && [ "$UPDATE2_RC" -eq 0 ] && [ "$COUNT1" -gt 0 ] && [ "$COUNT1" = "$COUNT2" ]; then
  _ok "migrations apply once and second run is a no-op"
else
  _fail "migration idempotency failed: rc1=$UPDATE1_RC rc2=$UPDATE2_RC count1=$COUNT1 count2=$COUNT2"
fi

echo ""
echo "--- 4. local config edits are reported and preserved ---"
CFG_PROJECT="$TMPROOT/config-project"
mkdir -p "$CFG_PROJECT/.mini-ork/config"
printf 'local-only: true\n' > "$CFG_PROJECT/.mini-ork/config/agents.yaml"
BEFORE_CFG=$(cat "$CFG_PROJECT/.mini-ork/config/agents.yaml")
(
  cd "$CFG_PROJECT" || exit 1
  export MINI_ORK_HOME="$CFG_PROJECT/.mini-ork"
  export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
  mini-ork update --dry-run
) > "$TMPROOT/config.out" 2>&1
AFTER_CFG=$(cat "$CFG_PROJECT/.mini-ork/config/agents.yaml")
if grep -q "local-edited.*agents.yaml" "$TMPROOT/config.out" && [ "$BEFORE_CFG" = "$AFTER_CFG" ]; then
  _ok "local-edited config is reported and not overwritten"
else
  _fail "local-edited config was not preserved or reported"
fi

echo ""
echo "--- 5. --pull in non-git MINI_ORK_ROOT skips pull and continues ---"
PULL_PROJECT="$TMPROOT/pull-project"
FAKE_ROOT="$TMPROOT/fake-root"
mkdir -p "$PULL_PROJECT/.mini-ork" "$FAKE_ROOT/bin" "$FAKE_ROOT/config" "$FAKE_ROOT/db/migrations"
cp "$MINI_ORK_ROOT/bin/mini-ork-update" "$FAKE_ROOT/bin/mini-ork-update"
cat > "$FAKE_ROOT/db/init.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$(dirname "$MINI_ORK_DB")"
sqlite3 "$MINI_ORK_DB" "CREATE TABLE IF NOT EXISTS schema_migrations(filename TEXT PRIMARY KEY);"
SH
chmod +x "$FAKE_ROOT/db/init.sh"
(
  cd "$PULL_PROJECT" || exit 1
  export MINI_ORK_ROOT="$FAKE_ROOT"
  export MINI_ORK_HOME="$PULL_PROJECT/.mini-ork"
  export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
  bash "$FAKE_ROOT/bin/mini-ork-update" --pull
) > "$TMPROOT/pull.out" 2>&1
PULL_RC=$?
if [ "$PULL_RC" -eq 0 ] && grep -qi "skipping pull" "$TMPROOT/pull.out"; then
  _ok "--pull skips non-git root and completes update"
else
  _fail "--pull non-git behavior failed: rc=$PULL_RC out=$(cat "$TMPROOT/pull.out")"
fi

echo ""
echo "=== Results: $PASS OK  $FAIL FAIL ==="
[ "$FAIL" -eq 0 ]
