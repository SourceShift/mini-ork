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

  # v0.2-pt8 (D-01): prompt-cache flags. Source lane-helpers + emit
  # cache flags before claude --print. Anthropic prompt cache is 60-70%
  # input-token discount when system prompt is stable — was missing on
  # main dispatch path (only wired into reflection-refiner /
  # mutation-adversary / rubric-prescreen).
  local _cache_flags=()
  if [ -f "$MINI_ORK_ROOT/lib/lane-helpers.sh" ]; then
    # shellcheck source=lib/lane-helpers.sh
    source "$MINI_ORK_ROOT/lib/lane-helpers.sh" 2>/dev/null || true
    if declare -f mo_emit_cache_flags >/dev/null 2>&1; then
      mo_emit_cache_flags _cache_flags || true
    fi
  fi

  # v0.2-pt8 (D-04+D-15+D-10 ★★): switch to --output-format json so we
  # capture .total_cost_usd. Post-process extracts .result to out_file
  # + .total_cost_usd to ${out_file}.cost sidecar. Falls back to text
  # passthrough if jq fails (model not emitting JSON envelope) so
  # existing dispatches stay backward-compat. Disable opt-out via
  # MO_LLM_FORMAT=text.
  #
  # v0.2-pt23 (D-048 fix, 2026-06-01): when MO_TRACE_RICH=1, switch to
  # --output-format stream-json so we can additionally parse tool_use
  # events into a .tool-summary sidecar — populates tool_calls + files_read
  # for execution_traces (was hardcoded [] at bin/mini-ork-execute:240-241,
  # the single confirmed D-048 root cause per
  # .agentflow/mini-orch/handoffs/20260601-2100-minimax-gateway-perf-report.md).
  local _format="${MO_LLM_FORMAT:-json}"
  local _capture_trace="${MO_TRACE_RICH:-0}"
  if [ "$_capture_trace" = "1" ] && [ "$_format" = "json" ]; then
    _format="stream-json"
  fi
  local _raw_out="${out_file}.raw"

  if _mo_llm_is_executable "$model"; then
    # Executable wrapper: cl_codex.sh / cl_gemini.sh handle their own CLI
    # (these don't support --output-format json universally → keep text)
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

    # v0.2-pt23: stream-json mode requires --verbose per claude CLI contract.
    local _verbose_flag=()
    [ "$_format" = "stream-json" ] && _verbose_flag=(--verbose)

    if [[ -n "$TIMEOUT_CMD" ]]; then
      (
        set +u  # secrets file may reference unset vars
        [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
        source "$cl_script"
        "$TIMEOUT_CMD" --kill-after=60 "$timeout_s" claude \
          --print \
          --permission-mode bypassPermissions \
          --output-format "$_format" \
          "${_verbose_flag[@]}" \
          --max-turns "$max_turns" \
          "${_cache_flags[@]}" \
          "$prompt"
      ) > "$_raw_out" 2>"$err_log" || { local _rc=$?; mv "$_raw_out" "$out_file" 2>/dev/null; return $_rc; }
    else
      (
        set +u
        [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
        source "$cl_script"
        claude \
          --print \
          --permission-mode bypassPermissions \
          --output-format "$_format" \
          "${_verbose_flag[@]}" \
          --max-turns "$max_turns" \
          "${_cache_flags[@]}" \
          "$prompt"
      ) > "$_raw_out" 2>"$err_log" || { local _rc=$?; mv "$_raw_out" "$out_file" 2>/dev/null; return $_rc; }
    fi

    # v0.2-pt23: stream-json post-process — parse line-delimited events,
    # extract final .result + .total_cost_usd + tool_calls + files_read.
    if [ "$_format" = "stream-json" ]; then
      python3 - "$_raw_out" "$out_file" "$err_log" <<'PY' || { local _rc=$?; mv "$_raw_out" "$out_file" 2>/dev/null; return $_rc; }
import json, sys, os
raw_path, out_path, err_path = sys.argv[1:4]
result_text = None
total_cost_usd = 0.0
is_error_flag = False
api_error_status = None
tool_calls = []
files_read = []
files_written = []
session_id = None
try:
    with open(raw_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = obj.get('type')
            if et == 'result':
                result_text = obj.get('result')
                total_cost_usd = float(obj.get('total_cost_usd', 0.0) or 0.0)
                is_error_flag = bool(obj.get('is_error', False))
                api_error_status = obj.get('api_error_status')
                session_id = obj.get('session_id', session_id)
            elif et == 'system' and obj.get('subtype') == 'init':
                session_id = obj.get('session_id', session_id)
            elif et == 'assistant':
                msg = obj.get('message', {}) or {}
                for block in msg.get('content', []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'tool_use':
                        name = block.get('name', 'unknown')
                        inp = block.get('input', {}) or {}
                        tool_calls.append({'tool': name, 'input': inp})
                        if name == 'Read':
                            fp = inp.get('file_path')
                            if fp and fp not in files_read:
                                files_read.append(fp)
                        elif name in ('Write', 'Edit', 'NotebookEdit'):
                            fp = inp.get('file_path')
                            if fp and fp not in files_written:
                                files_written.append(fp)
except Exception as e:
    sys.stderr.write(f"stream-json post-process error: {e}\n")
    with open(err_path, 'a') as ef:
        ef.write(f"stream-json post-process error: {e}\n")
    sys.exit(2)

# is_error guard (same shape as v0.2-pt22, applied to stream-json result)
if is_error_flag:
    with open(err_path, 'a') as ef:
        ef.write(f"mo_llm_dispatch: provider returned is_error=true (api_status={api_error_status})\n")
        ef.write(f"result: {result_text or 'no error message'}\n")
    sys.exit(3)

with open(out_path, 'w') as f:
    f.write((result_text or '') + ('\n' if result_text and not result_text.endswith('\n') else ''))
with open(out_path + '.cost', 'w') as f:
    f.write(f"{total_cost_usd}\n")
with open(out_path + '.tool-summary', 'w') as f:
    json.dump({
        'session_id': session_id,
        'tool_calls': tool_calls,
        'files_read': files_read,
        'files_written': files_written,
    }, f)
PY
      local _post_rc=$?
      if [ $_post_rc -ne 0 ]; then
        rm -f "$_raw_out"
        return $_post_rc
      fi
      rm -f "$_raw_out"
      return 0
    fi

    # D-04 post-process: extract .result + .total_cost_usd from claude
    # JSON envelope. If jq fails or output isn't JSON (legacy/text mode),
    # pass through raw — backward-compat for any caller expecting raw text.
    if [ "$_format" = "json" ] && command -v jq >/dev/null 2>&1 && \
       jq -e . "$_raw_out" >/dev/null 2>&1; then
      # v0.2-pt22 (2026-06-01): detect wrapper-hides-error class.
      # Observed: MiniMax-M2.7 401 returned subtype:"success" + is_error:true
      # + result:"Not logged in · Please run /login". Without this guard the
      # error string flows downstream as a "successful" model response —
      # gradient_extract parses garbage, returns [], silent D-048 contributor.
      # Perf report: .agentflow/mini-orch/handoffs/20260601-2100-minimax-gateway-perf-report.md
      if jq -e '.is_error == true' "$_raw_out" >/dev/null 2>&1; then
        local _api_status _err_msg
        _api_status=$(jq -r '.api_error_status // "unknown"' "$_raw_out")
        _err_msg=$(jq -r '.result // "no error message"' "$_raw_out")
        {
          echo "mo_llm_dispatch: provider returned is_error=true (api_status=$_api_status)"
          echo "result: $_err_msg"
        } >> "$err_log"
        rm -f "$_raw_out"
        return 3
      fi
      jq -r '.result // .' "$_raw_out" > "$out_file"
      jq -r '.total_cost_usd // 0' "$_raw_out" > "${out_file}.cost" 2>/dev/null || true
      rm -f "$_raw_out"
    else
      mv "$_raw_out" "$out_file"
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

  # v0.2-pt7 (R10): cost circuit breaker. Check accumulated daily spend
  # against MO_DAILY_BUDGET_USD before dispatching. Default cap: $50/day.
  # Returns non-zero with `[cost_circuit_open]` marker if exceeded so the
  # caller's $? check trips, halting the run gracefully.
  if [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ]; then
    local _budget="${MO_DAILY_BUDGET_USD:-50}"
    local _spent_today
    _spent_today=$(python3 -c "
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
con.execute('PRAGMA busy_timeout=5000')
cutoff = int(time.time()) - 86400
try:
    row = con.execute('SELECT COALESCE(SUM(cost_usd),0) FROM task_runs WHERE created_at >= ?', (cutoff,)).fetchone()
    print(f'{row[0]:.4f}')
except sqlite3.OperationalError:
    print('0')
finally:
    con.close()
" "$MINI_ORK_DB" 2>/dev/null || echo "0")
    if python3 -c "import sys; sys.exit(0 if float('$_spent_today') >= float('$_budget') else 1)" 2>/dev/null; then
      echo "[cost_circuit_open] spent_today=\$$_spent_today budget=\$$_budget — halting dispatch" >&2
      return 42
    fi
  fi

  # Resolve model: explicit override > agents.yaml lane lookup > env default > sonnet
  local model="${model_override:-${MINI_ORK_DEFAULT_MODEL:-sonnet}}"
  if [ -z "$model_override" ] && [ -n "$node_type" ]; then
    # v0.2-pt8 (G-002+K-07+D-06 ★★★ triple-consensus): cache agents.yaml
    # lane resolution per-session. Was: every llm_dispatch call forked a
    # python3 process to yaml.safe_load + dict lookup. At 100K dispatches/
    # day = 100K python3 forks. Cache via bash assoc array keyed on
    # node_type → model.
    # v0.2-pt20 (W5 from refactor-audit synthesis Section 2.5): switch from
    # `declare -gA _MO_LANE_CACHE` (assoc array — process-local) to per-key
    # exported env vars so subshells `( _dispatch_node ) &` in parallel
    # dispatch INHERIT the cache instead of re-parsing yaml.safe_load each.
    # At MINI_ORK_MAX_PARALLEL=4, was forking 4 redundant python3
    # processes per node-type batch.
    #
    # Key sanitisation (K-11): hyphens in lane names like `research-synthesis`
    # would blow up bash variable assignment. Convert to `_MO_LANE_<UPPER>`
    # with hyphens → underscores.
    local _safe_key
    _safe_key="_MO_LANE_${node_type^^}"
    _safe_key="${_safe_key//-/_}"
    local _cached_model="${!_safe_key:-}"
    if [ -n "$_cached_model" ]; then
      model="$_cached_model"
    else
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
      # Cache the resolution via export — subshells inherit. Cover both keyed-
      # by-original-name (legacy callers) and keyed-by-safe-name (new path).
      export "$_safe_key=$model"
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
    # v0.2-pt8 (D-04 wiring): expose .cost sidecar to caller via well-known
    # path. mo_llm_dispatch writes ${out_file}.cost when JSON output parses;
    # publish to ${MINI_ORK_RUN_DIR}/.last-llm-cost so execute's
    # _d022_charge_node_cost can read real cost vs $0.01 placeholder.
    if [ -f "${out_file}.cost" ] && [ -n "${MINI_ORK_RUN_DIR:-}" ]; then
      cp "${out_file}.cost" "${MINI_ORK_RUN_DIR}/.last-llm-cost" 2>/dev/null || true
      rm -f "${out_file}.cost"
    fi
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
