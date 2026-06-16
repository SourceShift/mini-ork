#!/usr/bin/env bash
# subagent-spawn.sh — Claude `PreToolUse:Task` hook.
#
# Wire-up: in ~/.claude/settings.json (or project hook config):
#   "hooks": {
#     "PreToolUse": [
#       { "matcher": "Task", "command": "<repo>/.mini-ork/hooks/subagent-spawn.sh" }
#     ]
#   }
#
# Claude Code passes hook input as JSON on stdin. We extract the Task() args
# (subagent_type, description, prompt) and write a row to subagent_runs
# linking the spawn back to the mini-ork run that owns this Claude session.
#
# Linkage env (set by _worker-launcher.sh before exec'ing claude):
#   MINI_ORK_RUN_ID                  → parent_run_id
#   MINI_ORK_DISPATCH_ID             → parent_dispatch_id
#   MINI_ORK_PARENT_CLAUDE_SESSION   → parent_claude_session_id
# When mini-ork didn't spawn this session, the worker session id can also be
# read from CLAUDE_SESSION_ID (set by Claude itself in some contexts).
#
# Output: emit `{"continue": true}` so Claude proceeds with the Task call.
# Failures are silent — never block a real spawn because of a logging hook.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

payload=""
if [ ! -t 0 ]; then
  payload=$(cat 2>/dev/null || true)
fi

emit_continue() {
  printf '{"continue": true}\n'
}

[ -f "$DB" ] || { emit_continue; exit 0; }

# Resolve the linkage. Prefer mini-ork-injected env vars; fall back to
parent_run_id="${MINI_ORK_RUN_ID:-}"
parent_dispatch_id="${MINI_ORK_DISPATCH_ID:-}"
parent_session="${MINI_ORK_PARENT_CLAUDE_SESSION:-${CLAUDE_SESSION_ID:-}}"

if [ -z "$parent_session" ]; then
  emit_continue
  exit 0
fi

extract_field() {
  local field="$1"
  if [ -z "$payload" ]; then return; fi
  if command -v jq >/dev/null 2>&1; then
    echo "$payload" | jq -r ".tool_input.${field} // .params.${field} // empty" 2>/dev/null
  else
    echo "$payload" | sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -1
  fi
}

subagent_type=$(extract_field "subagent_type")
description=$(extract_field "description")
prompt=$(extract_field "prompt")
cwd="${PWD}"

prompt_excerpt="${prompt:0:240}"
description_excerpt="${description:0:240}"

esc_sql() { printf '%s' "${1:-}" | sed "s/'/''/g"; }

to_int_or_null() {
  case "$1" in
    ''|*[!0-9]*) printf 'NULL' ;;
    *)           printf '%s' "$1" ;;
  esac
}

run_sql=$(to_int_or_null "$parent_run_id")
dispatch_sql=$(to_int_or_null "$parent_dispatch_id")

sqlite3 "$DB" "
  INSERT INTO subagent_runs
    (parent_dispatch_id, parent_run_id, parent_claude_session_id,
     subagent_type, description, prompt_excerpt, status, cwd)
  VALUES
    ($dispatch_sql, $run_sql, '$(esc_sql "$parent_session")',
     '$(esc_sql "$subagent_type")', '$(esc_sql "$description_excerpt")',
     '$(esc_sql "$prompt_excerpt")', 'spawned', '$(esc_sql "$cwd")');
" 2>/dev/null || true

# Mirror to ContextNest so substrate sees mini-ork subagent spawns alongside
# direct Claude Code sessions. Fire-and-forget; CN being down never blocks.
# Restored 2026-06-16 after PR #18 silently dropped this hook augmentation
# (regression caught by scripts/smoke-cn-bridge.sh).
if [ -f "${MINI_ORK_ROOT}/lib/cn_client.sh" ]; then
  # shellcheck source=../lib/cn_client.sh
  source "${MINI_ORK_ROOT}/lib/cn_client.sh" 2>/dev/null || true
  if declare -f cn_hook_post >/dev/null 2>&1; then
    cn_hook_post "session_start" "miniork-spawn-${parent_session}-$(date +%s)" "$cwd" "" || true
  fi
fi

emit_continue
