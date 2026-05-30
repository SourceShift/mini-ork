#!/usr/bin/env bash
# mini-ork dispatch tracker — records each Claude-session-driven decompose
# into the `orch_dispatches` table so the dashboard can see them
# without conflating them with running workers.
#
# History: this used to write rows into `runs` with agent='mini-ork' and
# ended_at=NULL forever. Result: 3-day-old dispatches showed in the dashboard
# as "live runs". The runs table is for actual worker executions (with iters,
# worker.log, exit code) — dispatch records belong in their own table with a
# real status enum.
#
# Function names stay the same (mo_runs_open/update/close) so existing
# callers — dispatch.sh, auto-merge.sh — don't have to change. The id
# returned by mo_runs_open is now an orch_dispatches.id, not a runs.id.
#
# Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller exports: AGENTFLOW_DIR (compat alias for MINI_ORK_HOME path)

_MO_DB="${MINI_ORK_DB:-${AGENTFLOW_DB:-${AGENTFLOW_DIR:-}/state.db}}"

# One-shot defensive migration: best-effort upgrade of older state.db files
# that predate the orch_dispatches table. Canonical migration lives at
# .mini-ork/migrations/20260510-orch-lifecycle-and-subagents.sql; this
# CREATE IF NOT EXISTS lets installs that haven't run the migration runner
# still get the right shape. Idempotent.
mo_runs_ensure_schema() {
  for col in claude_session_id zellij_session_name; do
    sqlite3 "$_MO_DB" \
      "ALTER TABLE runs ADD COLUMN $col TEXT;" 2>/dev/null || true
  done
  sqlite3 "$_MO_DB" "
    CREATE TABLE IF NOT EXISTS orch_dispatches (
      id                  INTEGER PRIMARY KEY AUTOINCREMENT,
      parent_dispatch_id  INTEGER REFERENCES orch_dispatches(id),
      epic_id             TEXT NOT NULL,
      group_id            TEXT,
      dispatched_by       TEXT NOT NULL CHECK (dispatched_by IN
        ('claude-session','orchestrator','human-cli','scaffold')),
      claude_session_id   TEXT,
      zellij_session_name TEXT,
      kickoff_path        TEXT,
      run_dir             TEXT,
      status              TEXT NOT NULL CHECK (status IN
        ('pending','in_progress','fanned_out','completed','cancelled')),
      rationale           TEXT,
      created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
      updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
      closed_at           TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_orch_dispatches_epic    ON orch_dispatches(epic_id);
    CREATE INDEX IF NOT EXISTS idx_orch_dispatches_status  ON orch_dispatches(status);
    CREATE INDEX IF NOT EXISTS idx_orch_dispatches_session ON orch_dispatches(claude_session_id);
  " 2>/dev/null || true
}

# Best-effort lookup of the Claude session UUID for the zellij session
# that spawned this orch. The claude-status-writer.sh hook keeps a JSON
# file at ~/.claude/status/<zellij>.json with `session_id`.
mo_runs_resolve_claude_session_id() {
  local zellij="${ZELLIJ_SESSION_NAME:-${ZELLIJ:-}}"
  [ -z "$zellij" ] && return 0
  local status_file="$HOME/.claude/status/$zellij.json"
  [ -f "$status_file" ] || return 0
  if command -v jq >/dev/null 2>&1; then
    jq -r '.session_id // ""' "$status_file" 2>/dev/null
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("session_id",""))' "$status_file" 2>/dev/null
  else
    sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$status_file" | head -1
  fi
}

mo_runs_sql_escape() {
  printf '%s' "${1:-}" | sed "s/'/''/g"
}

# Open an orch_dispatches row at dispatch start.
# Returns the integer dispatch id on stdout. Existing callers stashed this
# as `run_id`; the contract is preserved so dispatch.sh / auto-merge.sh
# don't need to change.
# Args: epic worktree
mo_runs_open() {
  local epic="$1" worktree="$2"
  mo_runs_ensure_schema
  local branch
  branch=$(git -C "$worktree" symbolic-ref --short HEAD 2>/dev/null || echo "unknown")
  local run_dir="mini-ork/${JOB_ID:-unknown}/$epic/$(date -u +%Y%m%dT%H%M%S)"
  local claude_sid zellij_name
  claude_sid=$(mo_runs_resolve_claude_session_id || true)
  zellij_name="${ZELLIJ_SESSION_NAME:-${ZELLIJ:-}}"
  local claude_sid_sql zellij_sql
  if [ -n "$claude_sid" ]; then
    claude_sid_sql="'$(mo_runs_sql_escape "$claude_sid")'"
  else
    claude_sid_sql="NULL"
  fi
  if [ -n "$zellij_name" ]; then
    zellij_sql="'$(mo_runs_sql_escape "$zellij_name")'"
  else
    zellij_sql="NULL"
  fi
  local group_sql
  if [ -n "${JOB_ID:-}" ]; then
    group_sql="'$(mo_runs_sql_escape "$JOB_ID")'"
  else
    group_sql="NULL"
  fi
  sqlite3 "$_MO_DB" "
    INSERT INTO orch_dispatches
      (epic_id, group_id, dispatched_by, claude_session_id,
       zellij_session_name, run_dir, status)
    VALUES
      ('$(mo_runs_sql_escape "$epic")', $group_sql, 'claude-session',
       $claude_sid_sql, $zellij_sql,
       '$(mo_runs_sql_escape "$run_dir")', 'in_progress');
    SELECT last_insert_rowid();
  " 2>/dev/null | tail -1
}

# Periodic update — called after every iter completes. Bumps updated_at and
# stamps the latest verdict into rationale (audit trail; not used for
# scheduling).
# Args: dispatch_id verdict
mo_runs_update_progress() {
  local dispatch_id="$1" verdict="$2"
  [ -z "$dispatch_id" ] && return
  sqlite3 "$_MO_DB" "
    UPDATE orch_dispatches SET
      updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
      rationale  = COALESCE(rationale, '') ||
                   CASE WHEN rationale IS NULL OR rationale = '' THEN '' ELSE ' | ' END ||
                   'iter:' || '$(mo_runs_sql_escape "$verdict")'
    WHERE id = $dispatch_id;
  " 2>/dev/null
}

# Close the dispatch at end-of-run. Maps the worker's final verdict to the
# dispatch lifecycle status:
#   APPROVE / MERGED / SALVAGED  → completed
#   anything else                → cancelled
# Aggregated cost lives in mini_orch_sessions (read-side join), not duplicated.
# Args: dispatch_id epic final_verdict
mo_runs_close() {
  local dispatch_id="$1" epic="$2" final_verdict="$3"
  [ -z "$dispatch_id" ] && return
  local new_status
  case "$final_verdict" in
    APPROVE|MERGED|SALVAGED) new_status="completed" ;;
    *)                       new_status="cancelled" ;;
  esac
  sqlite3 "$_MO_DB" "
    UPDATE orch_dispatches SET
      status     = '$new_status',
      updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
      closed_at  = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
      rationale  = COALESCE(rationale, '') ||
                   CASE WHEN rationale IS NULL OR rationale = '' THEN '' ELSE ' | ' END ||
                   'final:' || '$(mo_runs_sql_escape "$final_verdict")'
    WHERE id = $dispatch_id;
  " 2>/dev/null
}
