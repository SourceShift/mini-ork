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
  # Feature-detect: claude CLIs older than ~2.1.5x reject unknown options,
  # so emitting the flag unconditionally breaks EVERY dispatch on that
  # machine (observed on a fresh install with claude 2.1.47). Probe --help
  # once per process and degrade to cache-miss instead of hard failure.
  if [ -z "${_MO_CACHE_FLAG_SUPPORTED:-}" ]; then
    if claude --help 2>/dev/null | grep -q -- '--exclude-dynamic-system-prompt-sections'; then
      _MO_CACHE_FLAG_SUPPORTED=1
    else
      _MO_CACHE_FLAG_SUPPORTED=0
    fi
  fi
  [ "$_MO_CACHE_FLAG_SUPPORTED" = "1" ] && out=(--exclude-dynamic-system-prompt-sections)
  return 0
}

# Assert that a resolved dispatch lane's family supports every capability in
# MO_LANE_REQUIRES_CAPABILITY. Empty requirements are a no-op.
#
# Args:
#   1: resolved lane name, e.g. "opus_lens" or "implementer"
#
# The lane is resolved through the same agents.yaml precedence as dispatch:
# $MINI_ORK_HOME/config/agents.yaml first, then repo config/agents.yaml.
mo_assert_lane_capability() {
  local lane="${1:-}"
  local required="${MO_LANE_REQUIRES_CAPABILITY:-}"
  [ -n "$required" ] || return 0
  [ -n "$lane" ] || {
    echo "lane" >&2
    return 1
  }

  local agents_yaml="${MINI_ORK_HOME:-.mini-ork}/config/agents.yaml"
  [ -f "$agents_yaml" ] || agents_yaml="$MINI_ORK_ROOT/config/agents.yaml"
  [ -f "$agents_yaml" ] || {
    echo "capabilities" >&2
    return 1
  }
  local fallback_agents_yaml="$MINI_ORK_ROOT/config/agents.yaml"
  [ -f "$fallback_agents_yaml" ] || fallback_agents_yaml="$agents_yaml"

  python3 - "$agents_yaml" "$fallback_agents_yaml" "$lane" "$required" <<'PY'
import sys
import yaml

path, fallback_path, lane, required_csv = sys.argv[1:5]
cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
fallback_cfg = yaml.safe_load(open(fallback_path, encoding="utf-8")) or {}
lanes = cfg.get("lanes") or {}
capabilities = cfg.get("capabilities") or fallback_cfg.get("capabilities") or {}
family = str(lanes.get(lane) or lane).strip()
family_caps = capabilities.get(family) or {}

missing = []
for raw in required_csv.split(","):
    name = raw.strip()
    if not name:
        continue
    if family_caps.get(name) is not True:
        missing.append(name)

if missing:
    print(missing[0], file=sys.stderr)
    sys.exit(1)
PY
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

# ─────────────────────────────────────────────────────────────────────
# mo_claude_print — canonical claude --print wrapper with permission
# bypass + cache flags + max-turns baked in. Direct callers should use
# this instead of invoking `claude --print` directly, so the permission
# flag can't be accidentally dropped.
#
# v0.2-pt17 (per upstream-improvement proposal 2026-06-01): same fix-
# class as D-2 in refactor-audit synthesis 2026-05-31 ("4 direct claude
# -p callers bypass the daily cost circuit breaker"). This wrapper +
# bin/mo-check-claude-invocations lint together close the gap upstream
# AND give downstream dispatchers a 1-line migration target:
#   claude --print "$prompt"   →   mo_claude_print "$prompt"
#
# Signature: mo_claude_print <prompt> [extra_args...]
# Env knobs:
#   MO_CLAUDE_MAX_TURNS         (default 40)
#   MO_CLAUDE_OUTPUT_FORMAT     (default text; set json for cost extract)
#   MO_PROMPT_CACHE_DISABLED    (=1 to skip cache flags)
#
# Returns claude's exit code; stdout = claude stdout, stderr = claude
# stderr. Caller redirects as needed.
#
# NOTE: this wrapper does NOT source provider env-files (cl_*.sh).
# Caller is expected to have already sourced the right provider in a
# subshell. This wrapper just owns the FLAG discipline.
mo_claude_print() {
  local prompt="${1:?prompt required}"
  shift || true

  local _max_turns="${MO_CLAUDE_MAX_TURNS:-40}"
  local _format="${MO_CLAUDE_OUTPUT_FORMAT:-text}"

  # Cache flags via mo_emit_cache_flags (defined above in this file).
  local -a _cache_flags=()
  if declare -f mo_emit_cache_flags >/dev/null 2>&1; then
    mo_emit_cache_flags _cache_flags || true
  fi

  claude \
    --print \
    --permission-mode bypassPermissions \
    --output-format "$_format" \
    --max-turns "$_max_turns" \
    "${_cache_flags[@]}" \
    "$@" \
    "$prompt"
}
