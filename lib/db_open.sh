#!/usr/bin/env bash
# lib/db_open.sh — shared DB-open primitive (v0.2-pt7, closes audit F-11/R1)
#
# Centralizes SQLite connection pragmas for bash sqlite3 CLI callers.
# `busy_timeout` is PER-CONNECTION (not persistent like journal_mode=WAL),
# so every sqlite3 invocation MUST set it or risk silent SQLITE_BUSY
# under concurrent worker access.
#
# Source this lib, then use `mo_sqlite <db> <sql...>` instead of raw
# `sqlite3 <db> <sql...>`. The wrapper prepends a busy_timeout pragma
# (default 5000ms; override via MO_SQLITE_BUSY_MS env).
#
# Python heredocs (sqlite3.connect) handle busy_timeout inline via
# `con.execute("PRAGMA busy_timeout=5000")` immediately after connect —
# this wrapper covers the bash CLI side only.

# shellcheck disable=SC2120
mo_sqlite() {
  local db="${1:?mo_sqlite: db path required}"
  shift
  local busy_ms="${MO_SQLITE_BUSY_MS:-5000}"
  # `-cmd` runs the pragma BEFORE the user's SQL statements / script.
  # Works with both `mo_sqlite db "SELECT ..."` and `mo_sqlite db < file.sql`.
  if [ "$#" -eq 0 ]; then
    sqlite3 -cmd "PRAGMA busy_timeout=${busy_ms};" "$db"
  else
    sqlite3 -cmd "PRAGMA busy_timeout=${busy_ms};" "$db" "$@"
  fi
}

# Emit the Python-side pragma snippet for use in heredocs.
# Usage in a heredoc-generating bash function:
#   python3 - <<PY
#   import sqlite3
#   con = sqlite3.connect(sys.argv[1])
#   $(mo_sqlite_py_pragmas)
#   ...
#   PY
mo_sqlite_py_pragmas() {
  local busy_ms="${MO_SQLITE_BUSY_MS:-5000}"
  printf 'con.execute("PRAGMA busy_timeout=%s")\n' "$busy_ms"
}
