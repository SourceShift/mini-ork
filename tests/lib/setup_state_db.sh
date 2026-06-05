#!/usr/bin/env bash
# tests/lib/setup_state_db.sh — apply mini-ork DB migrations against
# `$MINI_ORK_DB` so unit tests can source libs whose contract assumes
# `mini-ork init` ran first.
#
# Background: several libs (trace_store.sh, memory.sh, context_assembler.sh,
# and the others without `_ensure_table` helpers) removed their inline
# `CREATE TABLE IF NOT EXISTS` blocks after the 2026-06-01 D-039 fix —
# they rely on migrations 0001..NNNN having been applied via the
# `mini-ork init` entry-point. Unit tests that source the lib directly
# against a fresh mktemp DB skip that step and hit
# `sqlite3.OperationalError: no such table: execution_traces` (or
# similar) on the very first call.
#
# Public API:
#   test_apply_migrations              applies every db/migrations/*.sql
#                                      against $MINI_ORK_DB in lex order
#                                      (idempotent — `CREATE TABLE IF NOT
#                                      EXISTS` is the convention).
#
# Requires: bash, sqlite3, $MINI_ORK_ROOT pointing at the repo root.
# Reads:    $MINI_ORK_DB (path to the test SQLite file).

# shellcheck disable=SC2086

test_apply_migrations() {
  local root="${MINI_ORK_ROOT:?MINI_ORK_ROOT unset — set before sourcing}"
  local db="${MINI_ORK_DB:?MINI_ORK_DB unset — point at an isolated test sqlite}"
  local mig_dir="$root/db/migrations"

  if [ ! -d "$mig_dir" ]; then
    echo "test_apply_migrations: migrations dir not found at $mig_dir" >&2
    return 1
  fi

  local n=0
  for sql in "$mig_dir"/*.sql; do
    [ -f "$sql" ] || continue
    if sqlite3 "$db" < "$sql" 2>/dev/null; then
      n=$((n + 1))
    else
      # Best-effort: some migrations may legitimately fail on a fresh
      # DB if they reference rows added by earlier migrations; the
      # important ones (0001 core schema + 0010 benchmarks +
      # 0013 task_runs + 0014 execution_traces) are idempotent and
      # standalone. Don't fail the test suite on a single migration
      # warning — surface only if zero migrations applied.
      :
    fi
  done

  if [ "$n" -eq 0 ]; then
    echo "test_apply_migrations: zero migrations applied — DB may not be writable" >&2
    return 1
  fi
  return 0
}
