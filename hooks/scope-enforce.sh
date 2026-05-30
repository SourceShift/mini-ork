#!/usr/bin/env bash
# scope-enforce.sh — Claude PreToolUse:Edit / PreToolUse:Write hook that
# hard-denies file edits outside the per-epic scope manifest.
#
# OPT-IN INSTALLATION (manual; not auto-wired by mini-ork):
#
#   In ~/.claude/settings.json:
#
#     "hooks": {
#       "PreToolUse:Edit":  ".mini-ork/hooks/scope-enforce.sh",
#       "PreToolUse:Write": ".mini-ork/hooks/scope-enforce.sh"
#     }
#
# Caveat: Claude reads settings.json once at session start. After editing
# settings.json, restart any in-flight worker sessions to pick up the hook.
#
# Reads stdin: Claude tool-call envelope JSON
#   { "tool_input": { "file_path": "..." }, ... }
#
# Resolves the per-epic scope-manifest.json via:
#   1. MINI_ORK_RUN_DIR env (set by _worker-launcher.sh) →
#      <run_dir>/scope-manifest.json
#   2. MINI_ORK_DISPATCH_ID env → orch_dispatches.run_dir →
#      <run_dir>/scope-manifest.json
#   3. Legacy AGENTFLOW_RUN_DIR / AGENTFLOW_DISPATCH_ID fallback
#
# Decision:
#   - If file_path matches any allowlist glob → continue:true
#   - Else if file_path matches any denylist  → continue:false (blocked)
#   - Else if no manifest reachable           → continue:true (fail-open;
#     the model is not running under mini-ork supervision)
#
# Citation: Aethelgard / Beyond Static Sandboxing (arXiv:2604.11839).

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

ALLOW_OUTPUT() { printf '%s\n' '{"continue": true}'; }
DENY_OUTPUT() {
  local reason="$1"
  jq -n -c --arg r "$reason" '{continue: false, reason: $r}'
}

input=$(cat 2>/dev/null || true)
file_path=$(echo "$input" | jq -r '.tool_input.file_path // .tool_input.path // empty' 2>/dev/null)

[ -z "$file_path" ] && { ALLOW_OUTPUT; exit 0; }

# Resolve manifest — prefer new MINI_ORK_* env, fall back to AGENTFLOW_*
manifest=""
run_dir="${MINI_ORK_RUN_DIR:-${AGENTFLOW_RUN_DIR:-}}"
dispatch_id="${MINI_ORK_DISPATCH_ID:-${AGENTFLOW_DISPATCH_ID:-}}"

if [ -n "$run_dir" ] && [ -f "$run_dir/scope-manifest.json" ]; then
  manifest="$run_dir/scope-manifest.json"
elif [ -n "$dispatch_id" ]; then
  db="${MINI_ORK_DB:-${AGENTFLOW_DB:-${AGENTFLOW_DIR:-${MINI_ORK_HOME:-.mini-ork}}/state.db}}"
  if [ -f "$db" ]; then
    found_run_dir=$(sqlite3 "$db" \
      "SELECT run_dir FROM orch_dispatches WHERE id=$dispatch_id;" 2>/dev/null || true)
    if [ -n "$found_run_dir" ]; then
      agentflow_dir="${AGENTFLOW_DIR:-${MINI_ORK_HOME:-.mini-ork}}"
      candidate="${agentflow_dir}/$found_run_dir/scope-manifest.json"
      [ -f "$candidate" ] && manifest="$candidate"
    fi
  fi
fi

[ -z "$manifest" ] && { ALLOW_OUTPUT; exit 0; }

rel_path="$file_path"
if [ -n "${MO_WORKTREE:-}" ] && [[ "$file_path" == "$MO_WORKTREE"* ]]; then
  rel_path="${file_path#"$MO_WORKTREE"/}"
elif [[ "$file_path" == /* ]]; then
  rel_path=$(basename "$file_path")
fi

# Test allowlist (POSIX glob via case).
allow_count=$(jq -r '.allowlist | length' "$manifest" 2>/dev/null || echo 0)
if [ "${allow_count:-0}" -gt 0 ]; then
  while IFS= read -r pat; do
    [ -z "$pat" ] && continue
    # shellcheck disable=SC2254
    case "$rel_path" in
      $pat|"./$pat") ALLOW_OUTPUT; exit 0 ;;
    esac
  done < <(jq -r '.allowlist[]' "$manifest")
fi

# Test denylist.
deny_count=$(jq -r '.denylist | length' "$manifest" 2>/dev/null || echo 0)
if [ "${deny_count:-0}" -gt 0 ]; then
  while IFS= read -r pat; do
    [ -z "$pat" ] && continue
    case "$pat" in
      */) [[ "$rel_path" == "$pat"* ]] && {
        DENY_OUTPUT "scope-enforce: $rel_path is in denylist (matched dir prefix $pat). Write a scope-question to ${MINI_ORK_HOME:-.mini-ork}/INBOX/ if you genuinely need this."
        exit 0
      } ;;
    esac
    # shellcheck disable=SC2254
    case "$rel_path" in
      $pat) DENY_OUTPUT "scope-enforce: $rel_path is in denylist (matched $pat). Write a scope-question to ${MINI_ORK_HOME:-.mini-ork}/INBOX/ if you genuinely need this."
            exit 0 ;;
    esac
  done < <(jq -r '.denylist[]' "$manifest")
fi

# Allowlist non-empty but no match AND not in denylist either → out-of-scope.
if [ "${allow_count:-0}" -gt 0 ]; then
  DENY_OUTPUT "scope-enforce: $rel_path matches no allowlist pattern. Out of scope for this epic — write a scope-question to ${MINI_ORK_HOME:-.mini-ork}/INBOX/ if needed."
  exit 0
fi

# Allowlist empty (degenerate) → fail-open.
ALLOW_OUTPUT
