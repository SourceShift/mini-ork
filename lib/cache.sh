#!/usr/bin/env bash
# mini-ork session-reuse cache (Phase A T0).
#
# Stage-level memoization: each LLM stage emits a row keyed by
# (epic_id, iter, stage, input_hash). Re-dispatches with the same
# input bundle hit the cache and skip the LLM call.
#
# Source from dispatch.sh.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Caller exports: MINI_ORK_HOME, JOB_ID, REPO_ROOT

# ─── Schema bootstrap (idempotent) ──────────────────────────────────────
# Called once on first source. Creates the table if missing.
mo_cache_init_schema() {
  local _db="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
  sqlite3 "$_db" <<'SQL'
CREATE TABLE IF NOT EXISTS mini_orch_sessions (
  uuid           TEXT PRIMARY KEY,
  job_id         TEXT NOT NULL,
  epic_id        TEXT NOT NULL,
  iter           INTEGER NOT NULL,
  stage          TEXT NOT NULL CHECK (stage IN (
                   'spec-author','spec-reviewer','mutation-adversary',
                   'mutation-validator','rubric','worker','reviewer',
                   'bdd-runner','reflection-refiner'
                 )),
  input_hash     TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('running','success','failed','resumable')),
  output_path    TEXT,
  log_path       TEXT,
  cost_usd       NUMERIC,
  turns          INTEGER,
  duration_ms    INTEGER,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  expires_at     TEXT NOT NULL,
  reused_count   INTEGER NOT NULL DEFAULT 0,
  prompt_version TEXT
);
CREATE INDEX IF NOT EXISTS mos_lookup ON mini_orch_sessions (
  epic_id, iter, stage, input_hash, status, expires_at
);
CREATE INDEX IF NOT EXISTS mos_gc ON mini_orch_sessions (expires_at);
CREATE VIEW IF NOT EXISTS mini_orch_cache_stats AS
SELECT
  stage,
  COUNT(*) AS rows_total,
  SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS rows_success,
  SUM(reused_count) AS times_reused,
  ROUND(SUM(CASE WHEN reused_count > 0 THEN cost_usd * reused_count ELSE 0 END), 2) AS dollars_saved,
  ROUND(SUM(cost_usd), 2) AS dollars_spent
FROM mini_orch_sessions
GROUP BY stage;
SQL
}

# ─── Hashing ────────────────────────────────────────────────────────────
# Stage-specific input bundle is concatenated by caller. We just hash
# whatever stdin gives us.
#
# Usage:
#   hash=$(printf '%s\n%s' "$kickoff_body" "$feedback_body" | mo_cache_input_hash)
#
# Falls back to shasum on macOS where sha256sum isn't always present.
mo_cache_input_hash() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    shasum -a 256 | awk '{print $1}'
  fi
}

# Helper: combine multiple files OR strings into the canonical hash bundle.
mo_cache_hash_bundle() {
  local data=""
  for arg in "$@"; do
    if [ -f "$arg" ]; then
      data="${data}$(cat "$arg")"
    else
      data="${data}${arg}"
    fi
    data="${data}"$'\x1e'
  done
  printf '%s' "$data" | mo_cache_input_hash
}

# ─── Lookup ─────────────────────────────────────────────────────────────
# Returns the output_path on cache HIT (status=success and not expired).
# Empty on miss.
#
# Usage:
#   hit_path=$(mo_cache_lookup spec-author IM-A 1 "$hash")
#   if [ -n "$hit_path" ]; then ...
mo_cache_lookup() {
  local stage="$1" epic="$2" iter="$3" input_hash="$4"
  local _db="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
  sqlite3 "$_db" "
    SELECT output_path FROM mini_orch_sessions
    WHERE epic_id = '$epic'
      AND iter = $iter
      AND stage = '$stage'
      AND input_hash = '$input_hash'
      AND status = 'success'
      AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')
    ORDER BY updated_at DESC
    LIMIT 1;
  " 2>/dev/null
}

# Bump reused_count on the row that just served the hit.
mo_cache_record_hit() {
  local stage="$1" epic="$2" iter="$3" input_hash="$4"
  local _db="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
  sqlite3 "$_db" "
    UPDATE mini_orch_sessions
       SET reused_count = reused_count + 1,
           updated_at   = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE epic_id = '$epic'
       AND iter = $iter
       AND stage = '$stage'
       AND input_hash = '$input_hash'
       AND status = 'success';
  " 2>/dev/null
}

# ─── Emit ───────────────────────────────────────────────────────────────
# Insert a row at stage completion. expires_at defaults to now + 30 days.
#
# Usage:
#   mo_cache_emit spec-author IM-A 1 "$hash" success "$spec_path" "$log_path" 1.82 17 240000
mo_cache_emit() {
  local stage="$1" epic="$2" iter="$3" input_hash="$4" status="$5"
  local output_path="$6" log_path="$7"
  local cost_usd="${8:-0}" turns="${9:-0}" duration_ms="${10:-0}"
  local prompt_version="${11:-v1}"

  local uuid
  uuid=$(uuidgen 2>/dev/null || printf '%08x-%04x-%04x-%04x-%012x' \
    $((RANDOM*RANDOM)) $((RANDOM)) $((RANDOM)) $((RANDOM)) $((RANDOM*RANDOM)))

  local expires_at
  expires_at=$(python3 -c "
import datetime as d
print((d.datetime.utcnow() + d.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%fZ'))
" 2>/dev/null || date -u -v+30d +"%Y-%m-%dT%H:%M:%fZ" 2>/dev/null || echo "2099-01-01T00:00:00.000Z")

  local _db="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
  sqlite3 "$_db" "
    INSERT INTO mini_orch_sessions
      (uuid, job_id, epic_id, iter, stage, input_hash, status,
       output_path, log_path, cost_usd, turns, duration_ms,
       expires_at, prompt_version)
    VALUES
      ('$uuid', '${JOB_ID:-unknown}', '$epic', $iter, '$stage', '$input_hash', '$status',
       '$output_path', '$log_path', $cost_usd, $turns, $duration_ms,
       '$expires_at', '$prompt_version')
    ON CONFLICT (uuid) DO NOTHING;
  " 2>/dev/null
}

# ─── Convenience: extract cost + turns from a Claude stream-json log ────
# Returns three space-separated values: cost_usd turns duration_ms
# Emits "0 0 0" on parse failure.
mo_cache_costline_from_log() {
  local log_path="$1"
  if [ ! -f "$log_path" ]; then
    echo "0 0 0"
    return
  fi
  grep '"type":"result"' "$log_path" 2>/dev/null | tail -1 | jq -r '
    [(.total_cost_usd // 0), (.num_turns // 0), (.duration_ms // 0)] | @tsv
  ' 2>/dev/null | tr '\t' ' ' || echo "0 0 0"
}

# ─── GC ─────────────────────────────────────────────────────────────────
# Best-effort cleanup. Called at start of dispatch; cheap (indexed).
mo_cache_gc() {
  local _db="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
  sqlite3 "$_db" "
    DELETE FROM mini_orch_sessions
    WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now');
  " 2>/dev/null
}

# ─── Stats line for COMPLETION_REPORT.md ────────────────────────────────
mo_cache_run_summary() {
  local job="$1"
  local _db="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"
  sqlite3 "$_db" -column -header "
    SELECT
      stage,
      SUM(reused_count) AS reuses,
      ROUND(SUM(CASE WHEN reused_count > 0 THEN cost_usd * reused_count ELSE 0 END), 2) AS saved_usd
    FROM mini_orch_sessions
    WHERE job_id = '$job' AND reused_count > 0
    GROUP BY stage;
  " 2>/dev/null
}
