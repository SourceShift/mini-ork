#!/usr/bin/env bash
# subagent-prefetch.sh — Claude `UserPromptSubmit` hook for mini-ork workers.
#
# Wire-up: in mini-ork's worker-launched Claude config:
#   "hooks": {
#     "UserPromptSubmit": [
#       { "command": "<mini-ork>/hooks/subagent-prefetch.sh" }
#     ]
#   }
#
# Purpose: Before a worker subagent's first turn runs, fetch fresh
# ContextNest atoms relevant to the prompt + cwd and write them to a
# scratch file the worker prompt template can reference. Solves the
# "worker flies blind" problem flagged by StackPlanner (arxiv:2601.05890):
# workers ignore retrieved-once-by-planner context after a few turns.
#
# The hook ONLY fires on the FIRST UserPromptSubmit of a session — repeated
# fires would re-fetch every turn (wasteful, no signal change) unless the
# scratch file is older than CN_PREFETCH_REFRESH_SEC. Set =0 to refresh
# every turn (debug).
#
# Output: emit `{"continue": true}` so Claude proceeds normally. Failures
# (CN down, parse error, anything) are silent — never block the user.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export PYTHONPATH="$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
RUN_DIR="${MINI_ORK_RUN_DIR:-${MINI_ORK_HOME:-.mini-ork}/runs}"
REFRESH_SEC="${CN_PREFETCH_REFRESH_SEC:-1800}"  # 30 min default

emit_continue() { printf '{"continue": true}\n'; }

# Read hook payload (Claude pipes JSON on stdin).
payload=""
if [ ! -t 0 ]; then
  payload=$(cat 2>/dev/null || true)
fi

# Disable opt-out.
[ "${MO_DISABLE_CN:-0}" = "1" ] && { emit_continue; exit 0; }

extract_field() {
  local field="$1"
  [ -z "$payload" ] && return
  if command -v jq >/dev/null 2>&1; then
    echo "$payload" | jq -r ".${field} // empty" 2>/dev/null
  else
    echo "$payload" | sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -1
  fi
}

session_id=$(extract_field "session_id")
[ -z "$session_id" ] && session_id="${CLAUDE_SESSION_ID:-anon-$(date +%s)}"
prompt_text=$(extract_field "prompt")
[ -z "$prompt_text" ] && prompt_text=$(extract_field "user_prompt")
cwd="${PWD:-}"

# Scratch dir for this worker. Worker prompt templates can read from
# $MO_CN_PREFETCH_PATH (env we export to the worker via subagent-spawn,
# or the worker can construct the path itself from session_id).
parent_run="${MINI_ORK_RUN_ID:-noop}"
prefetch_dir="$RUN_DIR/$parent_run/cn_prefetch"
mkdir -p "$prefetch_dir" 2>/dev/null || { emit_continue; exit 0; }
prefetch_file="$prefetch_dir/${session_id}.md"

# Skip if recent. NB: we intentionally do NOT skip on file existence alone
# because the prompt may have shifted significantly turn-to-turn.
if [ -f "$prefetch_file" ] && [ "$REFRESH_SEC" -gt 0 ]; then
  if [ "$(($(date +%s) - $(stat -f %m "$prefetch_file" 2>/dev/null || stat -c %Y "$prefetch_file" 2>/dev/null || echo 0)))" -lt "$REFRESH_SEC" ]; then
    emit_continue
    exit 0
  fi
fi

# Query CN through the native bridge. The prompt is the richest query we have;
# the bridge caps it before semantic retrieval and never blocks this hook.
query="${prompt_text:0:1500}"
[ -z "$query" ] && { emit_continue; exit 0; }
python3 -m mini_ork.cli.cn_hook prefetch \
  --session "$session_id" --prompt "$query" --cwd "$cwd" --output "$prefetch_file" \
  >/dev/null 2>&1 || true

emit_continue
