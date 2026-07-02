#!/usr/bin/env bash
# mini-ork db init — applies all migrations in lexicographic order, idempotently.
# Usage: MINI_ORK_DB=/path/to/state.db ./db/init.sh
#        Or:  mini-ork init  (wrapper in bin/)
#
# Env vars:
#   MINI_ORK_DB   — path to SQLite DB (default: ${MINI_ORK_HOME:-.mini-ork}/state.db)
#   MINI_ORK_HOME — project root for the .mini-ork dir (default: current dir)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATIONS_DIR="${SCRIPT_DIR}/migrations"
VIEWS_DIR="${SCRIPT_DIR}/views"

# Resolve DB path
MINI_ORK_HOME="${MINI_ORK_HOME:-${PWD}}"
DB="${MINI_ORK_DB:-${MINI_ORK_HOME}/.mini-ork/state.db}"
# Export so migrations that shell out via `.read "|sh -c '… ${MINI_ORK_DB:?} …'"`
# (e.g. 0037/0041 grounded_rejections guards) always resolve to the DB being
# initialized — even when init.sh was invoked on the default path with no
# MINI_ORK_DB env set. Without this, those migrations abort with
# "MINI_ORK_DB: parameter null or not set" (review-40 HIGH).
export MINI_ORK_DB="$DB"

# Ensure parent directory exists
DB_DIR="$(dirname "$DB")"
mkdir -p "$DB_DIR"

echo "[mini-ork init] DB: $DB"

if [ -L "$DB" ]; then
  echo "[mini-ork init] ERROR: state DB path is a symlink; refusing to initialize: $DB" >&2
  exit 1
fi

# v0.2-pt7 (closes audit F-10/F-11/R1):
# Enable WAL journaling at init time. WAL is a PERSISTENT pragma
# (stored in the DB file header), so all subsequent opens inherit it
# without per-connection setup. This single change moves SQLite from
# DELETE journal mode (full exclusive lock on every write, serializing
# readers with writers) to WAL (concurrent reads during writes) — the
# audit's #1 highest-leverage fix (Opus R1, GLM F-10, F-11).
#
# Per-connection busy_timeout still needs setting at each open site
# (it's per-connection, not persistent) — handled in hot-path Python
# heredocs inline (trace_store/cache/runs-tracker).
sqlite3 "$DB" "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;" >/dev/null 2>&1 || true

# Guarded real-column repair (review_id=32 fix-1):
# Some DBs have migrations 0031/0032 marked applied but the learning columns
# are missing (sqlite_schema drift, partial apply, etc). We use pragma_table_info
# to check both table existence and column presence before issuing ADD COLUMN,
# making this idempotent across the full migration graph. The CREATE INDEX
# statements in migration 0039 now have guaranteed backing storage here,
# so 0039 no longer needs the writable_schema hack.
ensure_column() {
  local table="$1" column="$2" ddl="$3"
  local exists
  exists=$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='$table';" 2>/dev/null || true)
  [ "$exists" = "$table" ] || return 0
  local col_count
  col_count=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('$table') WHERE name='$column';" 2>/dev/null || echo "0")
  if [ "${col_count:-0}" = "0" ]; then
    sqlite3 "$DB" "ALTER TABLE $table ADD COLUMN $column $ddl;" 2>/dev/null || true
  fi
}
ensure_column "execution_traces" "process_reward" "REAL DEFAULT NULL"
ensure_column "agent_performance_memory" "relative_advantage" "REAL NOT NULL DEFAULT 0.0"

# Apply migrations + views via the checksummed, transactional runner
# (lib/migrate.sh): real sha256 checksums, one transaction per migration, and
# in-place rehash of the old placeholder checksums. See
# docs/_meta/migration-and-update-design.md.
# shellcheck source=../lib/migrate.sh
. "$SCRIPT_DIR/../lib/migrate.sh"
export MINI_ORK_DB="$DB"
export MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
mo_migrate_ensure_table "$DB"

# Legacy recovery (preserved from the old inline runner): a partial/manual apply
# can leave llm_calls.session_id present without 0018 marked. Mark it so the
# runner won't try to re-ADD the column (which would fail). Only fires when the
# column already exists; a fresh DB has no llm_calls table yet, so 0018 applies
# normally.
if sqlite3 "$DB" "PRAGMA table_info(llm_calls);" 2>/dev/null | awk -F'|' '$2=="session_id"{f=1} END{exit f?0:1}'; then
  sqlite3 "$DB" "CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id) WHERE session_id IS NOT NULL;" 2>/dev/null || true
  sqlite3 "$DB" "INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum) VALUES ('0018_llm_calls_session_id.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'recovered-existing-session-id');" 2>/dev/null || true
fi

mo_migrate_apply "$MIGRATIONS_DIR" || exit 1
if [ -d "$VIEWS_DIR" ]; then
  mo_migrate_apply "$VIEWS_DIR" || exit 1
else
  echo "  [info] No views dir found at $VIEWS_DIR — skipping"
fi

# Validate: at least 20 CREATE TABLE statements in final schema
table_count=$(sqlite3 "$DB" ".schema" | grep -c "CREATE TABLE")
if [ "$table_count" -lt 20 ]; then
  echo "[mini-ork init] ERROR: expected >= 20 tables, found ${table_count}. Aborting." >&2
  exit 1
fi
# After 0009–0012 are applied the count should be >= 45.
# Emit a warning (non-fatal) if the redesign migrations appear missing.
if [ "$table_count" -lt 45 ]; then
  echo "[mini-ork init] WARNING: expected >= 45 tables after full apply, found ${table_count}. Redesign migrations (0009–0012) may not have been applied yet."
fi

echo "[mini-ork init] Done. Tables: ${table_count}"
