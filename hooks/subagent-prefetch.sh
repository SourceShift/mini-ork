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
RUN_DIR="${MINI_ORK_RUN_DIR:-${MINI_ORK_HOME:-.mini-ork}/runs}"
REFRESH_SEC="${CN_PREFETCH_REFRESH_SEC:-1800}"  # 30 min default

emit_continue() { printf '{"continue": true}\n'; }

# Read hook payload (Claude pipes JSON on stdin).
payload=""
if [ ! -t 0 ]; then
  payload=$(cat 2>/dev/null || true)
fi

# Disable opt-out + cn_client guard.
[ "${MO_DISABLE_CN:-0}" = "1" ] && { emit_continue; exit 0; }
[ -f "$MINI_ORK_ROOT/lib/cn_client.sh" ] || { emit_continue; exit 0; }
# shellcheck source=../lib/cn_client.sh
source "$MINI_ORK_ROOT/lib/cn_client.sh" 2>/dev/null || { emit_continue; exit 0; }
declare -f cn_retrieve >/dev/null 2>&1 || { emit_continue; exit 0; }

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

# Query CN. The prompt is the richest query we have — embed it as-is and
# let CN's semantic retrieve do the work. Cap input to avoid huge POSTs.
query="${prompt_text:0:1500}"
[ -z "$query" ] && { emit_continue; exit 0; }

cn_available || { emit_continue; exit 0; }

hits_json=$(cn_retrieve "$query" 8 2>/dev/null) || { emit_continue; exit 0; }
rendered=$(cn_render_atoms_md "$hits_json" 6 2>/dev/null)

# Also pull recent inbox items + features touching this cwd. Cheap concurrent
# calls; each guarded by its own timeout in cn_client.
inbox_json=$(cn_inbox 5 2>/dev/null) || inbox_json="{}"
features_json=$(cn_features_recent "48h" "" 2>/dev/null) || features_json="{}"

# Write the prefetch file. Format is plain markdown — the worker prompt
# template just inlines it (or ignores when empty).
{
  printf '# ContextNest prefetch for session %s\n' "$session_id"
  printf '_Generated at %s by subagent-prefetch.sh._\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [ -n "$rendered" ]; then
    printf '## Relevant atoms (semantic retrieve)\n\n%s\n\n' "$rendered"
  fi
  # Compact inbox surfacing — actions only, no full payload.
  python3 - "$inbox_json" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
items = d.get("items") or d.get("inbox") or []
if not items:
    sys.exit(0)
print("## Attention inbox (recent)\n")
for it in items[:5]:
    kind = it.get("kind", "?")
    sid = (it.get("session_id") or it.get("id", ""))[:8]
    text = (it.get("content") or it.get("subject") or it.get("action") or "").strip().replace("\n", " ")
    if len(text) > 160:
        text = text[:157] + "..."
    print(f"- [{kind} {sid}] {text}")
print()
PY
  python3 - "$features_json" "$cwd" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
features = d.get("features") or d.get("items") or []
if not features:
    sys.exit(0)
cwd = sys.argv[2]
# Surface ALL recent features (worker may benefit from cross-project context
# too); flag those matching the current cwd as "this project".
print("## Recent features delivered (last 48h)\n")
for f in features[:8]:
    name = (f.get("feature") or f.get("name") or "").strip()
    layer = f.get("layer", "?")
    htt = (f.get("how_to_test") or "").strip()
    pcwd = (f.get("project_cwd") or "")
    here = " [this project]" if cwd and pcwd and (cwd in pcwd or pcwd in cwd) else ""
    print(f"- ({layer}){here} {name}")
    if htt:
        print(f"  test: {htt[:160]}")
print()
PY
} > "$prefetch_file" 2>/dev/null || true

emit_continue
