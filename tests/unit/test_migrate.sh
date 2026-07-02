#!/usr/bin/env bash
# Unit test for lib/migrate.sh — the versioned/checksummed/transactional
# migration runner. Uses small synthetic migrations in a temp dir so it's fast
# and precise.
set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT="$ROOT"
# shellcheck source=/dev/null
. "$ROOT/lib/migrate.sh"
PASS=0; FAIL=0
ok(){ echo "  [OK]   $1"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export MINI_ORK_DB="$TMP/state.db"
MIG="$TMP/migrations"; mkdir -p "$MIG"
_q(){ sqlite3 "$MINI_ORK_DB" "$1" 2>/dev/null; }
echo "── unit: lib/migrate.sh ──"

printf 'CREATE TABLE t1(id INTEGER);\n' > "$MIG/0001_a.sql"
printf 'CREATE TABLE t2(id INTEGER);\n' > "$MIG/0002_b.sql"

# 1. fresh apply
mo_migrate_apply "$MIG" >/dev/null 2>&1
[ "$(_q "SELECT COUNT(*) FROM schema_migrations WHERE filename LIKE '000%';")" = "2" ] && ok "both migrations applied + recorded" || bad "apply/record failed"
[ "$(_q "SELECT name FROM sqlite_master WHERE name='t1';")" = "t1" ] && ok "migration DDL ran (t1 exists)" || bad "t1 missing"

# 2. real sha256 checksum stored (64 hex chars, not a placeholder)
CS=$(_q "SELECT checksum FROM schema_migrations WHERE filename='0001_a.sql';")
[ "${#CS}" = "64" ] && ok "real sha256 checksum stored (len 64)" || bad "checksum not a real hash ($CS)"

# 3. re-apply is idempotent (no re-run, no error)
out=$(mo_migrate_apply "$MIG" 2>&1)
printf '%s' "$out" | grep -q "apply" && bad "re-apply wrongly re-ran a migration" || ok "re-apply is a no-op (idempotent)"

# 4. editing an applied migration => drift FAIL
printf 'CREATE TABLE t1(id INTEGER, extra TEXT);\n' > "$MIG/0001_a.sql"
mo_migrate_apply "$MIG" >/dev/null 2>&1 && bad "edited migration was NOT rejected" || ok "edited-after-apply migration rejected (drift guard)"
# ...unless drift is explicitly allowed
MO_MIGRATE_ALLOW_DRIFT=1 mo_migrate_apply "$MIG" >/dev/null 2>&1 && ok "MO_MIGRATE_ALLOW_DRIFT=1 bypasses drift" || bad "drift override did not work"
printf 'CREATE TABLE t1(id INTEGER);\n' > "$MIG/0001_a.sql"  # restore
# repair the drifted checksum row so later steps are clean
_q "UPDATE schema_migrations SET checksum=(SELECT '$(mo_migrate_checksum "$MIG/0001_a.sql")') WHERE filename='0001_a.sql';" >/dev/null 2>&1

# 5. legacy placeholder checksum => rehashed to the real value, never re-run
_q "UPDATE schema_migrations SET checksum='runner-applied' WHERE filename='0002_b.sql';" >/dev/null
out=$(mo_migrate_apply "$MIG" 2>&1)
printf '%s' "$out" | grep -q "rehash" && ok "legacy checksum rehashed" || bad "legacy checksum not rehashed"
NEWCS=$(_q "SELECT checksum FROM schema_migrations WHERE filename='0002_b.sql';")
[ "${#NEWCS}" = "64" ] && ok "rehashed to real sha256" || bad "rehash produced wrong value ($NEWCS)"

# 6. a failing migration rolls back — DB unchanged, not recorded
printf 'CREATE TABLE t3(id INTEGER);\nTHIS IS NOT VALID SQL;\n' > "$MIG/0003_bad.sql"
mo_migrate_apply "$MIG" >/dev/null 2>&1 && bad "bad migration reported success" || ok "bad migration returns non-zero"
[ -z "$(_q "SELECT name FROM sqlite_master WHERE name='t3';")" ] && ok "failed migration rolled back (t3 NOT created)" || bad "partial DDL committed despite failure"
[ "$(_q "SELECT COUNT(*) FROM schema_migrations WHERE filename='0003_bad.sql';")" = "0" ] && ok "failed migration not recorded" || bad "failed migration wrongly recorded"
rm -f "$MIG/0003_bad.sql"

# 7. status + verify
mo_migrate_status "$MIG" 2>&1 | grep -q "2 applied, 0 pending, 0 drifted" && ok "status reports 2 applied / 0 pending / 0 drifted" || bad "status output wrong"
mo_migrate_verify "$MIG" >/dev/null 2>&1 && ok "verify passes on a clean DB" || bad "verify failed on clean DB"

echo "── Results: $PASS OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
