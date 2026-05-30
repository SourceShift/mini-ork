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

# Resolve DB path
MINI_ORK_HOME="${MINI_ORK_HOME:-${PWD}}"
DB="${MINI_ORK_DB:-${MINI_ORK_HOME}/.mini-ork/state.db}"

# Ensure parent directory exists
DB_DIR="$(dirname "$DB")"
mkdir -p "$DB_DIR"

echo "[mini-ork init] DB: $DB"

# Apply each migration in lex order
for migration_file in $(ls "$MIGRATIONS_DIR"/*.sql | sort); do
  filename="$(basename "$migration_file")"

  # Check if already applied (schema_migrations table may not exist yet on first run)
  already_applied=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM schema_migrations WHERE filename='${filename}';" 2>/dev/null || echo "0")

  if [ "$already_applied" = "1" ]; then
    echo "  [skip] $filename — already applied"
  else
    echo "  [apply] $filename"
    sqlite3 "$DB" < "$migration_file"
    echo "  [ok]   $filename"
  fi
done

# Validate: at least 20 CREATE TABLE statements in final schema
table_count=$(sqlite3 "$DB" ".schema" | grep -c "CREATE TABLE")
if [ "$table_count" -lt 20 ]; then
  echo "[mini-ork init] ERROR: expected >= 20 tables, found ${table_count}. Aborting." >&2
  exit 1
fi

echo "[mini-ork init] Done. Tables: ${table_count}"
