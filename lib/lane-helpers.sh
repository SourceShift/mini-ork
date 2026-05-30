#!/usr/bin/env bash
# mini-ork lane helpers — shared predicates for lane-aware behavior.
#
# Source from dispatch.sh; not meant to run alone.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Returns 0 (true) if the lane is a "free" gateway lane the operator has a
# coding plan for — these lanes ignore the --max-budget-usd cap because there
# is no marginal cost. Returns 1 (false) otherwise.
#
# Currently free: glm, kimi, minimax (per user's confirmed coding plans).
# Paid (cap matters): opus, sonnet (Anthropic flat-plan or pay-per-use).
# 2026-05-12: deepseek lane retired — aliased to glm by lib resolvers.
mo_lane_is_free() {
  local lane="${1:-}"
  case "$lane" in
    glm|kimi|minimax) return 0 ;;
    *) return 1 ;;
  esac
}

# Emit a `--max-budget-usd <val>` flag pair (or empty) into a bash array.
# Args: <result_array_name> <lane> <default_budget_usd>
# Resolves the budget value from the named env var (caller passes the var
# default), then suppresses the flag entirely when the lane is free.
#
# Usage:
#   local budget_flag=()
#   mo_emit_budget_flag budget_flag "$lane" "${MO_SPEC_AUTHOR_BUDGET_USD:-0.80}"
#   claude -p "${budget_flag[@]}" --output-format stream-json ...
mo_emit_budget_flag() {
  local -n out="$1"
  local lane="$2"
  local val="$3"
  out=()
  if mo_lane_is_free "$lane"; then
    return 0
  fi
  out=(--max-budget-usd "$val")
}

# Emit prompt-caching flags into a bash array. The Claude Code CLI's
# `--exclude-dynamic-system-prompt-sections` flag moves per-machine
# bits (cwd, env info, memory paths, git status) from the SYSTEM prompt
# into the first USER message. Effect: the system prompt becomes
# byte-identical across calls (same model + same tool set), so
# Anthropic's prefix-match prompt cache HITS on subsequent calls within
# the 5-min default TTL.
#
# Without this flag every claude -p call has a different system prompt
# (cwd varies per worktree, git status varies per commit, env varies
# per env-script source) → cache miss every call → full input-token
# price every call.
#
# Cost math: ~70% reduction on cached prefix tokens (write 1.25× +
# read 0.1×) for the system+tools portion (~3KB per call). At 25
# spec-synth calls per 5-epic dispatch this is ~$0.50-1.50 saved on
# Anthropic-billed lanes (opus reviewer, sonnet escalations). Free
# lanes (glm/kimi/minimax) don't expose Anthropic-style caching, so
# no marginal benefit there — but no marginal harm either.
#
# Disable per-call with MO_PROMPT_CACHE_DISABLED=1.
#
# Usage:
#   local cache_flags=()
#   mo_emit_cache_flags cache_flags
#   claude -p "${cache_flags[@]}" "${budget_flag[@]}" --output-format stream-json ...
mo_emit_cache_flags() {
  local -n out="$1"
  out=()
  if [ "${MO_PROMPT_CACHE_DISABLED:-0}" = "1" ]; then
    return 0
  fi
  out=(--exclude-dynamic-system-prompt-sections)
}

# Aggregate prompt-cache usage across all *.log files in an iter-N dir.
# Sums cache_creation_input_tokens (writes) + cache_read_input_tokens (reads)
# + input_tokens (uncached) across every claude stream-json log in the dir.
# Writes <iter-dir>/cache-stats.json with totals + per-file breakdown.
#
# Args: <iter-dir>
# Writes: <iter-dir>/cache-stats.json
mo_aggregate_cache_stats() {
  local iter_dir="$1"
  [ -d "$iter_dir" ] || return 1
  local stats_file="$iter_dir/cache-stats.json"

  local creation=0 read=0 uncached=0 file_count=0
  local per_file_json="[]"
  for log in "$iter_dir"/*.log; do
    [ -f "$log" ] || continue
    # Extract last occurrence of each metric per log (covers stream-json
    # usage rollups). Sum across logs.
    local c r u
    c=$(grep -oE '"cache_creation_input_tokens":[0-9]+' "$log" 2>/dev/null \
        | awk -F: '{s+=$2} END{print s+0}')
    r=$(grep -oE '"cache_read_input_tokens":[0-9]+' "$log" 2>/dev/null \
        | awk -F: '{s+=$2} END{print s+0}')
    u=$(grep -oE '"input_tokens":[0-9]+' "$log" 2>/dev/null \
        | awk -F: '{s+=$2} END{print s+0}')
    creation=$((creation + c))
    read=$((read + r))
    uncached=$((uncached + u))
    file_count=$((file_count + 1))
    per_file_json=$(echo "$per_file_json" | jq \
      --arg name "$(basename "$log")" \
      --argjson c "$c" --argjson r "$r" --argjson u "$u" \
      '. + [{file:$name, cache_creation:$c, cache_read:$r, uncached:$u}]')
  done

  # Estimate $$ saved: cache_read tokens at ~0.1× vs full price (assume
  # ~$3/M input on Anthropic-billed lanes; free lanes don't benefit but
  # the estimate doesn't break — call site can ignore).
  local saved_usd
  saved_usd=$(awk -v r="$read" 'BEGIN{printf "%.4f", r * 0.9 * 3 / 1000000}')

  jq -n \
    --argjson c "$creation" --argjson r "$read" --argjson u "$uncached" \
    --argjson f "$file_count" \
    --arg saved "$saved_usd" \
    --argjson breakdown "$per_file_json" \
    '{
      cache_creation_tokens: $c,
      cache_read_tokens: $r,
      uncached_input_tokens: $u,
      hit_rate: (if ($c + $r + $u) > 0 then ($r / ($c + $r + $u)) else 0 end),
      estimated_usd_saved: ($saved | tonumber),
      log_files_scanned: $f,
      per_file: $breakdown
    }' > "$stats_file"
}
