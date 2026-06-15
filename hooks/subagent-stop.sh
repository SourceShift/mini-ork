#!/usr/bin/env bash
# subagent-stop.sh — Claude `SubagentStop` hook.
#
# Wire-up:
#   "hooks": {
#     "SubagentStop": [
#       { "command": "<repo>/.mini-ork/hooks/subagent-stop.sh" }
#     ]
#   }
#
# Closes the most recent matching subagent_runs row by parent session +
# subagent_type and stamps result_excerpt + child_claude_session_id when
# Claude provides them in the stop payload.
#
# Output: emit `{"continue": true}` so Claude proceeds normally.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

payload=""
if [ ! -t 0 ]; then
  payload=$(cat 2>/dev/null || true)
fi

emit_continue() { printf '{"continue": true}\n'; }
[ -f "$DB" ] || { emit_continue; exit 0; }

parent_session="${MINI_ORK_PARENT_CLAUDE_SESSION:-${CLAUDE_SESSION_ID:-}}"
[ -z "$parent_session" ] && { emit_continue; exit 0; }

extract_field() {
  local field="$1"
  if [ -z "$payload" ]; then return; fi
  if command -v jq >/dev/null 2>&1; then
    echo "$payload" | jq -r ".tool_input.${field} // .tool_response.${field} // .params.${field} // .result.${field} // empty" 2>/dev/null
  else
    echo "$payload" | sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -1
  fi
}

subagent_type=$(extract_field "subagent_type")
child_session=$(extract_field "session_id")
result=$(extract_field "result")
[ -z "$result" ] && result=$(extract_field "summary")
status_raw=$(extract_field "status")

case "$status_raw" in
  fail|failed|error)        new_status="failed" ;;
  cancel|cancelled|aborted) new_status="cancelled" ;;
  *)                        new_status="completed" ;;
esac

result_excerpt="${result:0:480}"
esc_sql() { printf '%s' "${1:-}" | sed "s/'/''/g"; }

match_clause="parent_claude_session_id = '$(esc_sql "$parent_session")' AND status = 'spawned'"
if [ -n "$subagent_type" ]; then
  match_clause="$match_clause AND subagent_type = '$(esc_sql "$subagent_type")'"
fi

child_set_sql=""
if [ -n "$child_session" ]; then
  child_set_sql=", child_claude_session_id = '$(esc_sql "$child_session")'"
fi

sqlite3 "$DB" "
  UPDATE subagent_runs SET
    status        = '$new_status',
    ended_at      = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    result_excerpt = '$(esc_sql "$result_excerpt")',
    duration_ms   = CAST(
      (strftime('%s','now') - strftime('%s', started_at)) * 1000 +
      (strftime('%f','now') - strftime('%f', started_at)) * 1000 AS INTEGER
    )
    $child_set_sql
  WHERE id = (
    SELECT id FROM subagent_runs
     WHERE $match_clause
     ORDER BY started_at DESC
     LIMIT 1
  );
" 2>/dev/null || true

# Mirror to ContextNest so the child subagent's transcript is ingested into
# the substrate. CN's /api/v1/cc/hook/subagent_stop tails the JSONL from the
# last-known offset and extracts memories/features. Fire-and-forget.
if [ -f "$MINI_ORK_ROOT/lib/cn_client.sh" ]; then
  # shellcheck source=../lib/cn_client.sh
  source "$MINI_ORK_ROOT/lib/cn_client.sh" 2>/dev/null || true
  if declare -f cn_hook_post >/dev/null 2>&1; then
    transcript_path=$(extract_field "transcript_path")
    [ -z "$transcript_path" ] && transcript_path=$(extract_field "transcript")
    target_session="${child_session:-$parent_session}"
    cn_hook_post "subagent_stop" "$target_session" "$PWD" "$transcript_path" || true
  fi
fi

emit_continue
