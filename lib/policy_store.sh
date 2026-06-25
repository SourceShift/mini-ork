#!/usr/bin/env bash
# lib/policy_store.sh — store-port seam over lib/db_open.sh (v0.2-pt36 RLM-3).
#
# Central backend selector for the brain libraries (lane_router,
# process_reward, gradient_extractor). The SAME learning-loop implementation
# targets SQLite (dev / eng-team, default) AND Postgres (serving /
# book-gen, per-tenant) by sourcing this lib and going through
# mo_store_assert_sqlite / mo_store_db_path / mo_store_py_connect_snippet
# / mo_store_py_pragmas_snippet.
#
# Backend selection:
#   MO_STORE_BACKEND=sqlite    (default; preserves eng-team behavior)
#   MO_STORE_BACKEND=postgres  (stub; fails predictably until the real
#                              researcher-side PG impl lands)
#
# v0.2-pt36 deliberately stops short of a real PG implementation. The
# Postgres backend is wired ONLY enough to abort before any sqlite3 call,
# so a researcher landing the real PG writer can swap the stub without
# re-architecting callers. Eng-team SQLite behavior is unchanged.
#
# Public API:
#   mo_store_backend                 — print "sqlite" or "postgres"
#   mo_store_assert_sqlite           — die 78 (EX_CONFIG) if backend != sqlite
#   mo_store_db_path                 — print resolved DB path
#                                       (MO_STORE_DB → MINI_ORK_DB → default)
#   mo_store_py_connect_snippet      — emit backend-aware Python connect code
#                                       for use inside `python3 - <<PY` heredocs
#                                       (substituted via $(mo_store_py_connect_snippet))
#   mo_store_py_pragmas_snippet      — emit backend-aware Python pragma code
#                                       (same substitution shape)
#
# Conventions:
#   - DB path resolution is centralized here. lib/lane_router.sh and
#     lib/process_reward.sh use $(mo_store_db_path) instead of re-deriving
#     STATE_DB from MINI_ORK_DB / MINI_ORK_HOME / pwd.
#   - SQLite-direct callers (lane_router, process_reward today) call
#     mo_store_assert_sqlite at function entry. Postgres callers MUST
#     route through the Python snippets above; calling sqlite3 CLI or
#     sqlite3.connect directly while MO_STORE_BACKEND=postgres will hit
#     the assert and exit 78 before any DB call.
#   - lib/db_open.sh remains the low-level connection primitive
#     (mo_sqlite, mo_sqlite_py_pragmas). This seam adds policy, not
#     mechanics, on top.

# Strict mode only when executed directly — never leak set -u/pipefail onto a
# parent that sources this lib (matches lib/lane_router.sh / lib/process_reward.sh).
[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -uo pipefail

# mo_store_backend — print current backend name (sqlite|postgres).
# Unknown values fail fast with EX_USAGE (64) so a typo doesn't silently
# fall back to SQLite and mask a misconfigured deployment.
mo_store_backend() {
  local b="${MO_STORE_BACKEND:-sqlite}"
  case "$b" in
    sqlite)   printf '%s\n' sqlite ;;
    postgres) printf '%s\n' postgres ;;
    *)
      printf 'policy_store: unknown MO_STORE_BACKEND=%s (expected sqlite|postgres)\n' \
        "$b" >&2
      return 64  # EX_USAGE
      ;;
  esac
}

# mo_store_db_path — single source of truth for the DB file path.
# Resolution order matches the historical STATE_DB convention in
# lane_router / process_reward so SQLite-default callers see no change:
#   MO_STORE_DB  →  MINI_ORK_DB  →  ${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db
mo_store_db_path() {
  if [ -n "${MO_STORE_DB:-}" ]; then
    printf '%s\n' "$MO_STORE_DB"
  elif [ -n "${MINI_ORK_DB:-}" ]; then
    printf '%s\n' "$MINI_ORK_DB"
  elif [ -n "${MINI_ORK_HOME:-}" ]; then
    printf '%s\n' "$MINI_ORK_HOME/state.db"
  else
    printf '%s\n' "$(pwd)/.mini-ork/state.db"
  fi
}

# mo_store_assert_sqlite — gate SQLite-direct callers behind the seam.
# Returns 78 (EX_CONFIG, "configuration error") when a non-sqlite backend
# is selected so a postgres caller never silently executes against
# the local .mini-ork/state.db. The error message names the env var so
# the fix is obvious.
mo_store_assert_sqlite() {
  local b
  b=$(mo_store_backend)
  if [ "$b" != "sqlite" ]; then
    printf 'policy_store: backend=%s is a stub in v0.2-pt36; real impl lands researcher-side. Set MO_STORE_BACKEND=sqlite for default behavior.\n' \
      "$b" >&2
    return 78  # EX_CONFIG
  fi
}

# mo_store_py_connect_snippet — emit backend-aware Python connect code.
# Intended for substitution via $(mo_store_py_connect_snippet) inside an
# UNQUOTED `python3 - <<PY` heredoc (or capture into a variable and pass
# via stdin). SQLite path emits the canonical connect; postgres path
# emits a SystemExit before any DB call so the real PG impl can replace
# the stub by editing this single function.
mo_store_py_connect_snippet() {
  local b
  b=$(mo_store_backend)
  case "$b" in
    sqlite)
      printf 'import sqlite3\n'
      printf 'con = sqlite3.connect(db)\n'
      ;;
    postgres)
      printf 'raise SystemExit("policy_store: backend=postgres is a stub in v0.2-pt36 (researcher-side impl pending). Aborting before any PG call.")\n'
      ;;
  esac
}

# mo_store_py_pragmas_snippet — emit backend-aware Python pragma code.
# For SQLite this mirrors mo_sqlite_py_pragmas from lib/db_open.sh so
# busy_timeout stays PER-CONNECTION (see F-11/R1 audit note there).
# For postgres it's a no-op — the connect snippet above aborts before
# this line runs, so callers don't need to know.
mo_store_py_pragmas_snippet() {
  local b
  b=$(mo_store_backend)
  case "$b" in
    sqlite)
      local busy_ms="${MO_SQLITE_BUSY_MS:-5000}"
      printf 'con.execute("PRAGMA busy_timeout=%s")\n' "$busy_ms"
      ;;
    postgres)
      : # no-op; mo_store_py_connect_snippet already aborted
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "policy_store.sh — source me and call mo_store_backend / mo_store_assert_sqlite / mo_store_db_path / mo_store_py_connect_snippet / mo_store_py_pragmas_snippet"
fi