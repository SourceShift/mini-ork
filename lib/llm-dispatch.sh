#!/usr/bin/env bash
# llm-dispatch.sh — uniform LLM dispatcher for all v2/v3 stages + layers.
#
# Handles the two cl_*.sh shapes:
#   - cl_codex.sh / cl_gemini.sh — proper executables (have shebang, call
#     their respective CLI directly). Invoke directly with flags.
#   - cl_sonnet.sh / cl_kimi.sh / cl_glm.sh / cl_minimax.sh / cl_opus.sh —
#     sourceable env-export scripts that pin ANTHROPIC_* env vars. Must be
#     SOURCED in a subshell then `claude` invoked separately.
#
# Public API:
#   mo_llm_dispatch <model> <prompt-text> <output-file> [timeout_s] [max_turns]
#
# Returns: 0 on success (output captured in output-file), non-zero on failure.
# Stderr captured to <output-file>.err.log.
#
# Examples:
#   mo_llm_dispatch sonnet "$(cat prompt.md)" out.txt 1500 60
#   mo_llm_dispatch codex  "$(cat prompt.md)" out.txt 1500

set -euo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Models that ship as proper executables (call their CLI directly)
_MO_LLM_EXECUTABLE_MODELS=(codex gemini)

_mo_llm_is_executable() {
  local model="$1"
  for m in "${_MO_LLM_EXECUTABLE_MODELS[@]}"; do
    [[ "$m" == "$model" ]] && return 0
  done
  return 1
}

# mo_llm_dispatch <model> <prompt> <out_file> [timeout_s] [max_turns]
mo_llm_dispatch() {
  local model="${1:?model required}"
  local prompt="${2:?prompt required}"
  local out_file="${3:?out file required}"
  local timeout_s="${4:-1500}"
  local max_turns="${5:-60}"

  local scripts_dir="$MINI_ORK_ROOT/lib/providers"
  local cl_script="$scripts_dir/cl_${model}.sh"
  local err_log="${out_file}.err.log"

  if [[ ! -f "$cl_script" ]]; then
    echo "mo_llm_dispatch: cl_${model}.sh missing at $cl_script" >> "$err_log"
    return 2
  fi

  # Pick timeout binary (macOS may need gtimeout from coreutils)
  local TIMEOUT_CMD=""
  if command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_CMD="gtimeout"
  elif command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD="timeout"
  fi

  if _mo_llm_is_executable "$model"; then
    # Executable wrapper: cl_codex.sh / cl_gemini.sh handle their own CLI
    if [[ -n "$TIMEOUT_CMD" ]]; then
      "$TIMEOUT_CMD" --kill-after=60 "$timeout_s" \
        "$cl_script" --print --output-format text "$prompt" \
        > "$out_file" 2>"$err_log" || return $?
    else
      "$cl_script" --print --output-format text "$prompt" \
        > "$out_file" 2>"$err_log" || return $?
    fi
  else
    # Sourceable env-export: must run claude in subshell with cl_*.sh sourced
    local secrets="${MINI_ORK_SECRETS:-${MINI_ORK_HOME:-.mini-ork}/config/secrets.local.sh}"

    if [[ -n "$TIMEOUT_CMD" ]]; then
      (
        set +u  # secrets file may reference unset vars
        [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
        source "$cl_script"
        "$TIMEOUT_CMD" --kill-after=60 "$timeout_s" claude \
          --print \
          --permission-mode bypassPermissions \
          --output-format text \
          --max-turns "$max_turns" \
          "$prompt"
      ) > "$out_file" 2>"$err_log" || return $?
    else
      (
        set +u
        [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
        source "$cl_script"
        claude \
          --print \
          --permission-mode bypassPermissions \
          --output-format text \
          --max-turns "$max_turns" \
          "$prompt"
      ) > "$out_file" 2>"$err_log" || return $?
    fi
  fi
  return 0
}

# mo_llm_smoke <model> — cheap ping to verify auth + dispatcher works
mo_llm_smoke() {
  local model="${1:?model required}"
  local tmp_out; tmp_out=$(mktemp -t mo-llm-smoke.XXXXXX)
  if mo_llm_dispatch "$model" "Reply with exactly: PONG_${model^^}" "$tmp_out" 60 5; then
    if grep -qi "pong" "$tmp_out"; then
      echo "OK"
      rm -f "$tmp_out" "${tmp_out}.err.log"
      return 0
    fi
  fi
  echo "FAIL"
  echo "  --- stdout ---"
  head -3 "$tmp_out" 2>/dev/null | sed 's/^/  /'
  echo "  --- stderr ---"
  head -3 "${tmp_out}.err.log" 2>/dev/null | sed 's/^/  /'
  rm -f "$tmp_out" "${tmp_out}.err.log"
  return 1
}

# When invoked directly: smoke-test all inspectors
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  for m in opus sonnet kimi glm codex; do
    printf "  cl_%-7s ... " "$m"
    mo_llm_smoke "$m" || true
  done
fi
