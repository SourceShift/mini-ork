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

# ─────────────────────────────────────────────────────────────────────────────
# Universal-loop flag-based shim — fixes audit finding D-007.
#
# bin/mini-ork-{plan,execute,invoke-prompt} call `llm_dispatch` with
# --task-class X --node-type Y --prompt-text Z (returning text on stdout).
# The legacy mo_llm_dispatch uses positional <model> <prompt> <out-file>.
# This shim translates between them.
#
# Resolves model from $MINI_ORK_HOME/config/agents.yaml lanes.<node-type>
# (falling back to lanes.worker, then $MINI_ORK_DEFAULT_MODEL, then sonnet).
# ─────────────────────────────────────────────────────────────────────────────
llm_dispatch() {
  local task_class="" node_type="" prompt_text="" out_file="" model_override=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task-class)  task_class="$2";     shift 2 ;;
      --node-type)   node_type="$2";      shift 2 ;;
      --prompt-text) prompt_text="$2";    shift 2 ;;
      --out)         out_file="$2";       shift 2 ;;
      --model)       model_override="$2"; shift 2 ;;
      *)             shift ;;
    esac
  done

  # Resolve model: explicit override > agents.yaml lane lookup > env default > sonnet
  local model="${model_override:-${MINI_ORK_DEFAULT_MODEL:-sonnet}}"
  if [ -z "$model_override" ] && [ -n "$node_type" ]; then
    local _agents_yaml="${MINI_ORK_HOME:-.mini-ork}/config/agents.yaml"
    [ ! -f "$_agents_yaml" ] && _agents_yaml="$MINI_ORK_ROOT/config/agents.yaml"
    if [ -f "$_agents_yaml" ]; then
      local _resolved
      _resolved=$(python3 - "$_agents_yaml" "$node_type" 2>/dev/null <<'PY'
import sys, yaml
try:
    d = yaml.safe_load(open(sys.argv[1])) or {}
    lanes = d.get('lanes', {})
    print(lanes.get(sys.argv[2]) or lanes.get('worker') or lanes.get('worker_default') or 'sonnet')
except Exception:
    print('sonnet')
PY
      )
      [ -n "$_resolved" ] && model="$_resolved"
    fi
  fi

  # Allocate tmp out-file when caller wants stdout (default for universal-loop)
  local _tmp_out=""
  if [ -z "$out_file" ]; then
    _tmp_out=$(mktemp -t mo-llm-XXXXXX)
    out_file="$_tmp_out"
  fi

  # D-014: capture stderr to .err.log alongside out-file so failure causes
  # (rate limit / auth / model unavailable / prompt too long) are diagnosable.
  # mo_llm_dispatch already writes its own .err.log via convention, but our
  # outer wrapper captures the same stream explicitly here.
  local _err_file="${out_file}.shim.err"

  # Dispatch via legacy positional API; capture stderr; emit captured stdout.
  if mo_llm_dispatch "$model" "$prompt_text" "$out_file" >/dev/null 2>"$_err_file"; then
    cat "$out_file"
    # D-013: clean tmp out-file ONLY on success. The .err is empty here.
    [ -n "$_tmp_out" ] && rm -f "$_tmp_out"
    rm -f "$_err_file"
    return 0
  else
    local rc=$?
    # D-014: surface last 20 lines of claude CLI stderr to caller's stderr
    # so the framework's caller can see the actual error, not just rc=1.
    if [ -s "$_err_file" ] || [ -s "${out_file}.err.log" ]; then
      echo "[llm_dispatch FAIL model=${model} rc=${rc}]" >&2
      [ -s "$_err_file" ] && tail -20 "$_err_file" >&2
      [ -s "${out_file}.err.log" ] && tail -20 "${out_file}.err.log" >&2
    fi
    # D-013: PRESERVE tmp_out + err.log on failure for forensics.
    # Move to runs/<run>/llm-failure-<ts>.* so they survive shim cleanup.
    if [ -n "$_tmp_out" ] && [ -n "${MINI_ORK_RUN_ID:-}" ] && [ -n "${MINI_ORK_HOME:-}" ]; then
      local _forensic_dir="${MINI_ORK_HOME}/runs/${MINI_ORK_RUN_ID}/llm-failures"
      mkdir -p "$_forensic_dir" 2>/dev/null
      local _ts; _ts=$(date +%s)
      mv "$_tmp_out"  "$_forensic_dir/${_ts}-${model}.out"  2>/dev/null || rm -f "$_tmp_out"
      [ -f "$_err_file" ]               && mv "$_err_file"               "$_forensic_dir/${_ts}-${model}.shim.err" 2>/dev/null
      [ -f "${out_file}.err.log" ]      && mv "${out_file}.err.log"      "$_forensic_dir/${_ts}-${model}.err.log"  2>/dev/null
      echo "[llm_dispatch forensics → $_forensic_dir/${_ts}-${model}.*]" >&2
    elif [ -n "$_tmp_out" ]; then
      # No run-dir to preserve into; at least leave on tmp + tell caller
      echo "[llm_dispatch forensics retained at $_tmp_out (no MINI_ORK_RUN_ID/HOME set)]" >&2
    fi
    return "$rc"
  fi
}
