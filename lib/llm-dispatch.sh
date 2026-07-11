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

# providers.yaml registry — BYO-key providers without a cl_*.sh wrapper.
# Precedence: an existing cl_<model>.sh ALWAYS wins; the registry is only
# consulted for model names with no wrapper file. See lib/providers/registry.sh.
# shellcheck source=lib/providers/registry.sh
[ -f "$MINI_ORK_ROOT/lib/providers/registry.sh" ] && \
  source "$MINI_ORK_ROOT/lib/providers/registry.sh"

# Daily budget cap single source of truth: config/agents.yaml budget.daily_cap_usd
# (read via mo_daily_budget_cap). The cost circuit below calls it instead of
# hardcoding 50 so the cap matches the conductor + agents.yaml.
# shellcheck source=lib/budget_config.sh
[ -f "$MINI_ORK_ROOT/lib/budget_config.sh" ] && \
  source "$MINI_ORK_ROOT/lib/budget_config.sh"

# Models that ship as proper executables (call their CLI directly)
_MO_LLM_EXECUTABLE_MODELS=(codex gemini)

_mo_llm_is_executable() {
  local model="$1"
  local _m
  for _m in "${_MO_LLM_EXECUTABLE_MODELS[@]}"; do
    [[ "$_m" == "$model" ]] && return 0
  done
  return 1
}

_mo_llm_now_ms() {
  if command -v gdate >/dev/null 2>&1; then
    gdate +%s%3N
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import time; print(int(time.time() * 1000))'
  else
    echo "MISSING_TIME_SHIM: install coreutils gdate or python3" >&2
    return 127
  fi
}

_mo_llm_write_duration_ms() {
  local duration_ms="${1:-0}"
  [ -n "${MINI_ORK_RUN_DIR:-}" ] || return 0
  printf '%s\n' "$duration_ms" > "${MINI_ORK_RUN_DIR}/.last-llm-duration-ms" 2>/dev/null || true
}

# _mo_check_lane_fuse <lane> <error_category>
#
# Returns 1 when MO_FUSE_ENABLED=1 and the last three completed calls for the
# lane all failed with the same retryable error_category. Returns 0 otherwise
# so callers can proceed. The lane is stored in llm_calls.feature_name by the
# flag-based shim as "<task_class>:<lane>".
_mo_check_lane_fuse() {
  local lane="${1:-}" error_category="${2:-}"
  [ "${MO_FUSE_ENABLED:-0}" = "1" ] || return 0
  [ -n "$lane" ] || return 0
  [ -n "$error_category" ] || return 0
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] || return 0

  python3 - "$MINI_ORK_DB" "$lane" "$error_category" <<'PY'
import json
import sqlite3
import sys

db, lane, category = sys.argv[1:4]
feature_suffix = ":" + lane
con = sqlite3.connect(db, timeout=5.0)
con.execute("PRAGMA busy_timeout = 5000")
try:
    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    if not {"status", "feature_name", "error_category", "retryable"} <= cols:
        sys.exit(0)
    rows = con.execute(
        """
        SELECT status, error_category, retryable
          FROM llm_calls
         WHERE feature_name LIKE ?
         ORDER BY id DESC
         LIMIT 3
        """,
        (f"%{feature_suffix}",),
    ).fetchall()
finally:
    con.close()

if len(rows) != 3:
    sys.exit(0)
trip = all(
    status == "failed" and err == category and int(retryable or 0) == 1
    for status, err, retryable in rows
)
if trip:
    print(json.dumps({"lane": lane, "error_category": category, "consecutive_failures": 3}))
    sys.exit(1)
sys.exit(0)
PY
}

_mo_record_lane_fuse_trip() {
  local lane="${1:-}" category="${2:-}"
  [ -n "$lane" ] || return 0
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] || return 0
  [ -n "${MINI_ORK_TASK_RUN_ID:-${MINI_ORK_RUN_ID:-}}" ] || return 0

  python3 - "$MINI_ORK_DB" "${MINI_ORK_TASK_RUN_ID:-$MINI_ORK_RUN_ID}" "$lane" "$category" <<'PY' 2>/dev/null || true
import sqlite3
import sys
import time

db, run_id, lane, category = sys.argv[1:5]
con = sqlite3.connect(db, timeout=5.0)
con.execute("PRAGMA busy_timeout = 5000")
try:
    cols = {r[1] for r in con.execute("PRAGMA table_info(task_runs)").fetchall()}
    if {"fuse_blown_lane", "fuse_consecutive_failures"} <= cols:
        con.execute(
            """
            UPDATE task_runs
               SET fuse_blown_lane = ?,
                   fuse_consecutive_failures = 3,
                   updated_at = COALESCE(?, updated_at)
             WHERE id = ?
            """,
            (lane, int(time.time()), run_id),
        )
        con.commit()
finally:
    con.close()
PY
}

# Redact API-key-shaped tokens from provider error strings before they hit
# the llm_calls.error_message column or the operator's stderr. The column is
# exposed via the read-only web API (mini_ork/web/agents.py:565), so an
# unredacted 401 echo like "Invalid x-api-key: sk-ant-abc123…" would expose
# partial credentials to any caller of /api/runs/<id>/llm-calls.
#
# Patterns cover the prefixes mini-ork providers actually use today:
#   sk-ant-…, sk-or-…, sk-lf-…, sk-…  (Anthropic / OpenRouter / Langfuse / generic)
#   Bearer <token>                     (HTTP header echoes from verbose curl)
#   ANTHROPIC_AUTH_TOKEN=…/etc         (env dumps)
#   32+ hex chars                      (GLM-style raw keys, cl_glm.sh:9)
_mo_llm_redact_secrets() {
  local s="${1:-}"
  [ -z "$s" ] && { printf '%s' ""; return; }
  printf '%s' "$s" | sed -E \
    -e 's/sk-[a-zA-Z]{1,6}-[A-Za-z0-9_-]{8,}/[REDACTED_KEY]/g' \
    -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED_KEY]/g' \
    -e 's/[Bb]earer[[:space:]]+[A-Za-z0-9_.+\/=-]{8,}/Bearer [REDACTED]/g' \
    -e 's/(ANTHROPIC_(AUTH_TOKEN|API_KEY)|[A-Z_]+_API_KEY)=[^[:space:]"'"'"']+/\1=[REDACTED]/g' \
    -e 's/[a-fA-F0-9]{32,}/[REDACTED_HEX]/g'
}

_mo_llm_classify_error() {
  local message="${1:-}" rc="${2:-}"
  local text
  text=$(printf '%s' "$message" | tr '[:upper:]' '[:lower:]')

  case "$rc" in
    6|7|28) printf 'network\n'; return 0 ;;
  esac

  if printf '%s' "$text" | grep -Eq 'missing (api key|wrapper)|api key.*missing|malformed config|config.*missing|cl_[a-z0-9_-]+\.sh missing|no providers\.yaml entry|\$[A-Z0-9_]+ is empty'; then
    printf 'config\n'
  elif printf '%s' "$text" | grep -Eq '(^|[^0-9])(401|403)([^0-9]|$)|invalid api key|authentication failed|not logged in|unauthorized|forbidden'; then
    printf 'auth\n'
  elif printf '%s' "$text" | grep -Eq '429' && printf '%s' "$text" | grep -Eq 'monthly|tokens-per-day|billing|quota|insufficient credits|credit limit'; then
    printf 'quota\n'
  elif printf '%s' "$text" | grep -Eq '(^|[^0-9])(429|503)([^0-9]|$)' && printf '%s' "$text" | grep -Eq 'capacity|concurrent|rate|overload|temporarily unavailable'; then
    printf 'capacity\n'
  elif printf '%s' "$text" | grep -Eq '(^|[^0-9])400([^0-9]|$)|invalid request|context too long|maximum context|prompt too long'; then
    printf 'request\n'
  elif printf '%s' "$text" | grep -Eq '(^|[^0-9])422([^0-9]|$)|content filter|safety|moderation'; then
    printf 'safety\n'
  elif printf '%s' "$text" | grep -Eq 'connection refused|could not resolve|dns|timed out|timeout was reached|network is unreachable'; then
    printf 'network\n'
  elif printf '%s' "$text" | grep -Eq 'partial stream|unexpected eof|stream.*(closed|ended|error)|incomplete chunk|chunked encoding'; then
    printf 'stream\n'
  elif printf '%s' "$text" | grep -Eq '(^|[^0-9])(500|502|504)([^0-9]|$)|internal server error|bad gateway|gateway timeout|provider error'; then
    printf 'provider\n'
  else
    printf 'unknown\n'
  fi
}

_mo_llm_error_retryable() {
  case "${1:-unknown}" in
    capacity|network|stream|provider) printf '1\n' ;;
    *) printf '0\n' ;;
  esac
}

_mo_llm_write_llm_calls_row() {
  # Args (positional):
  #   1=provider 2=model_id 3=tier 4=feature_name 5=actor
  #   6=status   7=duration_ms 8=cost_usd 9=error_message
  #   10=input_tokens (optional) 11=output_tokens (optional)
  #   12=metadata_json (optional) — per-turn extras like turn_index, session_id
  #   13=cached_input_tokens (optional) 14=cache_creation_input_tokens (optional)
  local provider="$1" model_id="$2" tier="$3" feature_name="$4"
  local actor="$5" status="$6" duration_ms="$7" cost_usd="$8" error_message="$9"
  local input_tokens="${10:-0}" output_tokens="${11:-0}" metadata_json="${12:-{\}}"
  local cached_input_tokens="${13:-0}" cache_creation_input_tokens="${14:-0}"
  local error_category="" retryable=""
  if [ "$status" = "failed" ]; then
    error_category=$(_mo_llm_classify_error "$error_message" "${MO_LLM_LAST_RC:-}")
    retryable=$(_mo_llm_error_retryable "$error_category")
  fi
  # Always include MO_NODE_ID in metadata for UI attribution — the
  # recipe's actual node name (e.g. perf_lens, opus_synthesizer), distinct
  # from lane/family (e.g. minimax_lens, opus_lens) which appears in
  # feature_name. Lens nodes share lanes so feature_name alone is
  # ambiguous; node_id is the unique key.
  if [ -n "${MO_NODE_ID:-}" ]; then
    metadata_json=$(MO_NODE_ID="$MO_NODE_ID" python3 -c '
import json, os, sys
md = sys.argv[1] or "{}"
try: d = json.loads(md)
except Exception: d = {}
if not isinstance(d, dict): d = {}
d["node_id"] = os.environ.get("MO_NODE_ID", "")
print(json.dumps(d))' "$metadata_json")
  fi
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "$MINI_ORK_DB" ] || return 0

  local iter="${MO_RECURSIVE_ITER:-}"
  local run_id="${MINI_ORK_RUN_ID:-}"
  local traceparent="${MO_TRACEPARENT:-}"
  # Auto-derive traceparent from the task_runs row if env wasn't set —
  # covers bin/mini-ork-plan + bin/mini-ork-classify (and anywhere else)
  # that doesn't explicitly export MO_TRACEPARENT. The dispatcher does
  # export it after reading task_runs.trace_id, but earlier stages
  # (classify writes the row, plan runs before execute) need this fallback.
  if [ -z "$traceparent" ] && [ -n "${MINI_ORK_TASK_RUN_ID:-}" ] && [ -f "${MINI_ORK_DB:-}" ]; then
    local _tid
    _tid=$(sqlite3 "$MINI_ORK_DB" "SELECT COALESCE(trace_id,'') FROM task_runs WHERE id='${MINI_ORK_TASK_RUN_ID}' LIMIT 1;" 2>/dev/null)
    if [ -n "$_tid" ]; then
      traceparent="00-${_tid}-$(printf '%016x' $((RANDOM * RANDOM + $$)))-01"
    fi
  fi
  local err_dir="${MINI_ORK_RUN_DIR:-/tmp}"
  mkdir -p "$err_dir" 2>/dev/null || err_dir="/tmp"

  python3 - "$MINI_ORK_DB" "$provider" "$model_id" "$tier" "$feature_name" \
    "$actor" "$status" "$duration_ms" "$cost_usd" "$error_message" \
    "$iter" "$run_id" "$traceparent" "$input_tokens" "$output_tokens" "$metadata_json" \
    "$cached_input_tokens" "$cache_creation_input_tokens" "$error_category" "$retryable" \
    <<'PY' 2>>"${err_dir}/trace-write-errors.log" || true
import sqlite3
import sys

db, *args = sys.argv[1:]
con = sqlite3.connect(db, timeout=5)
con.execute("PRAGMA busy_timeout=5000")
in_tok = int(args[12] or 0)
out_tok = int(args[13] or 0)
cached_in = int(args[15] or 0)
cache_create = int(args[16] or 0)
uncached_in = max(in_tok - cached_in - cache_create, 0)
cost_input_uncached = uncached_in * 15.0 / 1_000_000
cost_input_cached = cached_in * 1.5 / 1_000_000
cost_cache_write = cache_create * 18.75 / 1_000_000
error_category = args[17] or None
retryable = int(args[18]) if args[18] != "" else None
import json as _json
try:
    _md = _json.loads(args[14] or "{}")
    _sess = _md.get("session_id") if isinstance(_md, dict) else None
except Exception:
    _sess = None
cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
insert_cols = [
    "provider", "model_id", "tier", "feature_name", "actor",
    "status", "duration_ms", "cost_usd", "error_message", "iter",
    "run_id", "traceparent", "input_tokens", "output_tokens",
    "total_tokens", "metadata_json", "session_id",
]
values = [
    args[0], args[1], args[2], args[3], args[4], args[5],
    int(args[6] or 0), float(args[7] or 0.0), args[8] or None,
    int(args[9]) if args[9] else None, args[10] or None,
    args[11] or None, in_tok, out_tok, in_tok + out_tok,
    args[14] or "{}", _sess,
]
if "error_category" in cols:
    insert_cols.append("error_category")
    values.append(error_category)
if "retryable" in cols:
    insert_cols.append("retryable")
    values.append(retryable)
if "cached_input_tokens" in cols:
    insert_cols.append("cached_input_tokens")
    values.append(cached_in)
if "cache_creation_input_tokens" in cols:
    insert_cols.append("cache_creation_input_tokens")
    values.append(cache_create)
if "cost_input_uncached_usd" in cols:
    insert_cols.append("cost_input_uncached_usd")
    values.append(cost_input_uncached)
if "cost_input_cached_usd" in cols:
    insert_cols.append("cost_input_cached_usd")
    values.append(cost_input_cached)
if "cost_cache_write_usd" in cols:
    insert_cols.append("cost_cache_write_usd")
    values.append(cost_cache_write)
placeholders = ",".join("?" for _ in insert_cols)
con.execute(
    f"INSERT INTO llm_calls ({', '.join(insert_cols)}) VALUES ({placeholders})",
    values,
)
con.commit()
con.close()
PY
}

_mo_llm_provider_for_model() {
  # Registry family wins for registry-defined providers (telemetry accuracy
  # for BYO endpoints); falls back to the historical name-pattern heuristic.
  if declare -f mo_provider_field >/dev/null 2>&1; then
    local _fam
    _fam=$(mo_provider_field "$1" family 2>/dev/null) || _fam=""
    if [ -n "$_fam" ]; then
      printf '%s\n' "$_fam"
      return 0
    fi
  fi
  case "$1" in
    codex|gpt-*|o1*|o3*) printf 'openai\n' ;;
    gemini*|*-gemini-*) printf 'google\n' ;;
    minimax*|glm*|kimi*|deepseek*) printf 'gateway\n' ;;
    *) printf 'anthropic\n' ;;
  esac
}

# Strip session-protocol blocks that dispatched CLIs inherit from the
# OPERATOR's global agent config (~/.claude/CLAUDE.md and similar) and emit
# into their deliverable output. Observed (D-016 family, run-1781095892-69202):
# codex implementer appended a <z-insight>{...}</z-insight> block after its
# JSON envelope — downstream parsers and the UI transcript both showed it.
# The plan-side balanced-brace extractor tolerates it; everything else
# shouldn't have to. Sanitize at the dispatch boundary so every consumer
# (envelope parsers, transcripts, run artifacts) sees the assistant body only.
_mo_llm_strip_protocol_blocks() {
  local out_file="${1:?out_file required}"
  [ -f "$out_file" ] || return 0
  grep -q '<z-insight>' "$out_file" 2>/dev/null || return 0
  python3 - "$out_file" <<'PY' 2>/dev/null || true
import re, sys
path = sys.argv[1]
with open(path, encoding="utf-8", errors="replace") as f:
    text = f.read()
cleaned = re.sub(r"<z-insight>.*?</z-insight>", "", text, flags=re.S)
# Unterminated block (output truncated mid-emission): drop the tail.
cleaned = re.sub(r"<z-insight>.*\Z", "", cleaned, flags=re.S)
if cleaned != text:
    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned.rstrip() + "\n")
PY
}

# Transcript writer for executable lanes (cl_codex.sh / cl_gemini.sh).
# Merges the wrapper's MO_TURNS_FILE sidecar (real per-turn token usage from
# codex --json turn.completed events) with the final output text, so the UI
# shows real tokens instead of the zero-token "text-output fallback" card.
# Falls back to _mo_llm_write_text_transcript when no sidecar was produced.
# (Bug observed run-1781095892-69202: llm_calls had 77020 in / 1143 out from
# the sidecar, but the transcript carried zeros — two consumers, one wired.)
_mo_llm_write_exec_transcript() {
  local out_file="${1:?out_file required}"
  local model="${2:-unknown}"
  [ -f "$out_file" ] || return 0
  [ -f "${out_file}.transcript.json" ] && return 0
  if [ ! -s "${out_file}.turns.jsonl" ]; then
    _mo_llm_write_text_transcript "$out_file" "$model"
    return 0
  fi

  python3 - "$out_file" "$model" <<'PY' 2>/dev/null || _mo_llm_write_text_transcript "$out_file" "$model"
import json
import os
import sys

out_path, model = sys.argv[1:3]
max_bytes = int(os.environ.get("MO_MAX_TRANSCRIPT_BYTES", "1048576"))
turns = []
total_in = total_out = 0
with open(out_path + ".turns.jsonl", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        t_in = int(t.get("input_tokens") or 0)
        t_out = int(t.get("output_tokens") or 0)
        total_in += t_in
        total_out += t_out
        turns.append({
            "turn_index": len(turns),
            "model": t.get("model") or model,
            "input_tokens": t_in,
            "output_tokens": t_out,
            "text": t.get("text") or "",
            "tool_uses": t.get("tool_uses") or [],
            "cache_read_input_tokens": int(t.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(t.get("cache_creation_input_tokens") or 0),
            "stop_reason": t.get("stop_reason"),
            "session_id": t.get("session_id"),
        })
if not turns:
    sys.exit(1)  # caller falls back to the plain-text writer

# Wrappers emit usage-only turn lines; the assistant body lives in out_file.
# Attach it to the LAST turn (codex exec surfaces the final agent_message).
try:
    with open(out_path, encoding="utf-8", errors="replace") as f:
        text = f.read(max_bytes + 1)
except OSError:
    text = ""
truncated = len(text.encode("utf-8", errors="replace")) > max_bytes
if truncated:
    text = text[: max(200, max_bytes // 4)] + "\n...[truncated]"
if text and not turns[-1]["text"]:
    turns[-1]["text"] = text

payload = {
    "turns": turns,
    "totals": {"input_tokens": total_in, "output_tokens": total_out},
}
if truncated:
    payload["truncated"] = True
with open(out_path + ".transcript.json", "w", encoding="utf-8") as f:
    json.dump(payload, f)
PY
}

_mo_llm_write_text_transcript() {
  local out_file="${1:?out_file required}"
  local model="${2:-unknown}"
  [ -f "$out_file" ] || return 0
  [ -f "${out_file}.transcript.json" ] && return 0

  python3 - "$out_file" "$model" <<'PY' 2>/dev/null || true
import json
import os
import sys

out_path, model = sys.argv[1:3]
max_bytes = int(os.environ.get("MO_MAX_TRANSCRIPT_BYTES", "1048576"))
try:
    with open(out_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read(max_bytes + 1)
except OSError:
    sys.exit(0)

truncated = len(text.encode("utf-8", errors="replace")) > max_bytes
if truncated:
    text = text[:max(200, max_bytes // 4)] + "\n...[truncated]"

payload = {
    "turns": [
        {
            "turn_index": 0,
            "model": model,
            "input_tokens": 0,
            "output_tokens": 0,
            "text": text,
            "tool_uses": [],
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "stop_reason": None,
            "session_id": None,
        }
    ],
    "fallback": "text-output",
}
if truncated:
    payload["truncated"] = True
with open(out_path + ".transcript.json", "w", encoding="utf-8") as f:
    json.dump(payload, f)
PY
}

_mo_llm_persist_agent_transcript() {
  local out_file="${1:?out_file required}"
  local model="${2:-unknown}"
  [ -n "${MINI_ORK_RUN_DIR:-}" ] || return 0
  [ -n "${MO_NODE_ID:-}" ] || return 0
  [ -d "$MINI_ORK_RUN_DIR" ] || return 0

  if [ ! -f "${out_file}.transcript.json" ]; then
    _mo_llm_write_text_transcript "$out_file" "$model"
  fi
  [ -f "${out_file}.transcript.json" ] || return 0

  local safe_node
  safe_node=$(printf '%s' "$MO_NODE_ID" | tr -c 'A-Za-z0-9_.-' '_')
  cp "${out_file}.transcript.json" \
    "${MINI_ORK_RUN_DIR}/agent-${safe_node}.transcript.json" 2>/dev/null || true
  if [ -f "${out_file}.stream.jsonl" ]; then
    cp "${out_file}.stream.jsonl" \
      "${MINI_ORK_RUN_DIR}/agent-${safe_node}.stream.jsonl" 2>/dev/null || true
  fi

  # MIGRATION: remove when this node moves to mini_ork.dispatch
  # Minimal bash mirror for nodes not yet on the Python dispatch backend.
  # Registers run_artifacts rows (kind='turn_jsonl' + 'transcript') so the
  # schema lives in both worlds. Inline-python heredoc is PRAGMA-gated, so
  # it no-ops on DBs without the run_artifacts table (pre-0047 migrations).
  # call_id is intentionally NULL on the bash side: bash cannot cheaply
  # recover llm_calls.lastrowid across an inline-python boundary.
  if [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB}" ]; then
    MINI_ORK_DB="$MINI_ORK_DB" \
    MINI_ORK_RUN_DIR="$MINI_ORK_RUN_DIR" \
    MINI_ORK_RUN_ID="${MINI_ORK_RUN_ID:-}" \
    MO_NODE_ID="$MO_NODE_ID" \
    _safe_node="$safe_node" \
    python3 <<'PY' 2>/dev/null || true
import hashlib, os, sqlite3, time
db_path = os.environ["MINI_ORK_DB"]
run_dir = os.environ["MINI_ORK_RUN_DIR"]
run_id = os.environ.get("MINI_ORK_RUN_ID", "") or ""
node_id = os.environ.get("MO_NODE_ID", "") or ""
safe_node = os.environ.get("_safe_node", "")
if not (db_path and run_dir and run_id and node_id and safe_node):
    raise SystemExit(0)
try:
    con = sqlite3.connect(db_path, timeout=5)
except Exception:
    raise SystemExit(0)
try:
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "run_artifacts" not in tables:
        raise SystemExit(0)
    existing = {r[1] for r in con.execute("PRAGMA table_info(run_artifacts)")}
    cols = [c for c in ("run_id", "node_id", "call_id", "kind", "rel_path",
                        "bytes", "sha256", "created_at") if c in existing]
    ph = ",".join("?" for _ in cols)
    col_sql = ",".join(f'"{c}"' for c in cols)
    insert = f"INSERT OR IGNORE INTO run_artifacts ({col_sql}) VALUES ({ph})"
    now = int(time.time())
    for kind, fname in (
        ("turn_jsonl", f"agent-{safe_node}.stream.jsonl"),
        ("transcript", f"agent-{safe_node}.transcript.json"),
    ):
        p = os.path.join(run_dir, fname)
        if not os.path.isfile(p):
            continue
        try:
            size = os.path.getsize(p)
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            digest = h.hexdigest()
        except OSError:
            continue
        values = {
            "run_id": run_id, "node_id": node_id, "call_id": None,
            "kind": kind, "rel_path": fname, "bytes": size,
            "sha256": digest, "created_at": now,
        }
        try:
            con.execute(insert, [values[c] for c in cols])
        except sqlite3.IntegrityError:
            pass
    con.commit()
finally:
    con.close()
PY
  fi

  # MIGRATION: remove when this node moves to mini_ork.dispatch
  # Gzip on completion: turn every agent-<node>.stream.jsonl under the run
  # dir into a .stream.jsonl.gz sibling and rewrite the matching
  # run_artifacts row's rel_path/sha256/bytes. Self-contained inline python
  # (no mini_ork import — this mirror must survive a bare bash exec that
  # hasn't installed the package). PRAGMA-gated on run_artifacts; no-op on
  # old DBs.
  if [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB}" ] \
     && [ -d "${MINI_ORK_RUN_DIR:-}" ]; then
    MINI_ORK_DB="$MINI_ORK_DB" \
    MINI_ORK_RUN_DIR="$MINI_ORK_RUN_DIR" \
    MINI_ORK_RUN_ID="${MINI_ORK_RUN_ID:-}" \
    python3 <<'PY' 2>/dev/null || true
import glob, gzip, hashlib, os, sqlite3, time
db_path = os.environ["MINI_ORK_DB"]
run_dir = os.environ["MINI_ORK_RUN_DIR"]
run_id = os.environ.get("MINI_ORK_RUN_ID", "") or ""
if not run_id:
    # rel_path is a bare basename shared across runs; without a run_id to scope
    # the UPDATE we'd clobber other runs' rows. Skip rather than corrupt.
    raise SystemExit(0)
try:
    con = sqlite3.connect(db_path, timeout=5)
except Exception:
    raise SystemExit(0)
try:
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "run_artifacts" not in tables:
        raise SystemExit(0)
    now = int(time.time())
    gzipped = 0
    for src_path in sorted(glob.glob(os.path.join(run_dir, "agent-*.stream.jsonl"))):
        gz_path = src_path + ".gz"
        try:
            if os.path.exists(gz_path) and os.path.getmtime(gz_path) >= os.path.getmtime(src_path):
                continue
            with open(src_path, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=6) as fout:
                while True:
                    chunk = fin.read(65536)
                    if not chunk:
                        break
                    fout.write(chunk)
            size = os.path.getsize(gz_path)
            h = hashlib.sha256()
            with open(gz_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            rel_gz = os.path.basename(gz_path)
            rel_src = os.path.basename(src_path)
            con.execute(
                "UPDATE run_artifacts SET rel_path=?, bytes=?, sha256=?, created_at=? "
                "WHERE rel_path=? AND run_id=?",
                (rel_gz, size, digest, now, rel_src, run_id),
            )
            gzipped += 1
        except (OSError, sqlite3.Error):
            continue
    if gzipped:
        con.commit()
finally:
    con.close()
PY
  fi
}

# v0.2-pt38 (E-MO-19, 2026-06-02): models that route through non-Anthropic
# gateway endpoints. These don't stream `stream-json` events properly,
# so we downgrade their output format to `json` even when MO_TRACE_RICH=1.
_MO_LLM_GATEWAY_MODELS=(minimax glm kimi deepseek)

_mo_llm_is_gateway() {
  local model="$1"
  local _m
  for _m in "${_MO_LLM_GATEWAY_MODELS[@]}"; do
    [[ "$_m" == "$model" ]] && return 0
  done
  # Registry anthropic-compat providers are gateways by default (third-party
  # Anthropic-compatible endpoints don't stream `stream-json` reliably —
  # same hang class as E-MO-19). Opt out with `gateway: false`.
  if declare -f mo_provider_kind >/dev/null 2>&1; then
    local _kind _gw
    _kind=$(mo_provider_kind "$model" 2>/dev/null) || _kind=""
    if [ "$_kind" = "anthropic-compat" ]; then
      _gw=$(mo_provider_field "$model" gateway 2>/dev/null) || _gw=""
      [ "$_gw" != "false" ] && return 0
    fi
  fi
  return 1
}

# Apply a registry provider's env contract inside the dispatch subshell.
# Mirrors what the sourceable cl_*.sh wrappers do, driven by providers.yaml.
_mo_registry_apply_env() {
  local model="$1"
  local kind model_id base_url key_env
  kind=$(mo_provider_kind "$model") || return 1
  model_id=$(mo_provider_field "$model" model 2>/dev/null) || model_id=""
  case "$kind" in
    anthropic-native)
      # Same policy as cl_opus.sh (2026-06-09 incident fix): clear gateway
      # pollution so claude --print falls back to ambient Claude Code login.
      # BYO addition: when the operator exported ANTHROPIC_API_KEY (raw
      # Anthropic API key, no Claude Code login), preserve it and pin the
      # registry model id if one is declared.
      local _byo_key="${ANTHROPIC_API_KEY:-}"
      unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_BASE_URL \
            ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL \
            ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
            ANTHROPIC_SMALL_FAST_MODEL CLAUDE_CODE_SUBAGENT_MODEL \
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
      if [ -n "$_byo_key" ]; then
        export ANTHROPIC_API_KEY="$_byo_key"
        [ -n "$model_id" ] && export ANTHROPIC_MODEL="$model_id"
      fi
      ;;
    anthropic-compat)
      base_url=$(mo_provider_field "$model" base_url 2>/dev/null) || base_url=""
      key_env=$(mo_provider_field "$model" api_key_env 2>/dev/null) || key_env=""
      if [ -z "$base_url" ] || [ -z "$key_env" ]; then
        echo "[registry] provider '$model': anthropic-compat requires base_url + api_key_env" >&2
        return 1
      fi
      if [ -z "${!key_env:-}" ]; then
        echo "[registry] provider '$model': \$$key_env is empty — set it in secrets.local.sh or the environment" >&2
        return 1
      fi
      export ANTHROPIC_AUTH_TOKEN="${!key_env}"
      export ANTHROPIC_BASE_URL="$base_url"
      if [ -n "$model_id" ]; then
        export ANTHROPIC_MODEL="$model_id"
        export ANTHROPIC_SMALL_FAST_MODEL="$model_id"
        export ANTHROPIC_DEFAULT_OPUS_MODEL="$model_id"
        export ANTHROPIC_DEFAULT_SONNET_MODEL="$model_id"
        export ANTHROPIC_DEFAULT_HAIKU_MODEL="$model_id"
        export CLAUDE_CODE_SUBAGENT_MODEL="$model_id"
      fi
      export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
      ;;
    *)
      return 1
      ;;
  esac
  local _extra _line
  _extra=$(mo_provider_field "$model" extra_env 2>/dev/null) || _extra=""
  if [ -n "$_extra" ]; then
    while IFS= read -r _line; do
      [ -n "$_line" ] && export "$_line"
    done <<< "$_extra"
  fi
  return 0
}

# mo_llm_dispatch <model> <prompt> <out_file> [timeout_s] [max_turns]
mo_llm_dispatch() {
  local model="${1:?model required}"
  local prompt="${2:?prompt required}"
  local out_file="${3:?out file required}"
  local timeout_s="${4:-1500}"
  local max_turns="${5:-60}"

  # ADR-001 Phase 1: delegate to the Python dispatch layer when opted in
  # (MO_DISPATCH_BACKEND=python). The Python path reads the prompt over stdin
  # (E2BIG-proof), fails fast on a missing API key / framework-tree cwd, and
  # writes the same sidecar contract this bash path does (.last-llm-cost,
  # .last-llm-duration-ms, <out>.cost). PYTHONPATH pins the package without
  # changing cwd, so the provider still runs in the (guarded) target cwd.
  if [ "${MO_DISPATCH_BACKEND:-bash}" = "python" ]; then
    # Use the executor's role-aware fallback chain when it's for THIS model
    # (chain leads with $model), so a hung/flaky lead lane routes to the next
    # lane instead of stalling the run. Otherwise dispatch the single model.
    local _model_arg="$model"
    if [ -n "${MO_DISPATCH_CHAIN:-}" ] && [ "${MO_DISPATCH_CHAIN%%,*}" = "$model" ]; then
      _model_arg="$MO_DISPATCH_CHAIN"
    fi
    # Source secrets BEFORE delegating — the python layer reads API keys from
    # os.environ and (unlike the bash path below) never sources
    # secrets.local.sh itself. Without this every gateway lane fails preflight
    # with "key not set" whenever the caller relied on MINI_ORK_SECRETS
    # (2026-07-03 migration batch: 10/10 runs dead on this).
    local _py_secrets="${MINI_ORK_SECRETS:-${MINI_ORK_HOME:-.mini-ork}/config/secrets.local.sh}"
    if [ -f "$_py_secrets" ]; then
      set -a; # shellcheck disable=SC1090
      source "$_py_secrets" 2>/dev/null || true; set +a
    fi
    printf '%s' "$prompt" | PYTHONPATH="${MINI_ORK_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m mini_ork.dispatch "$_model_arg" --out "$out_file" --timeout "$timeout_s"
    return $?
  fi

  local scripts_dir="$MINI_ORK_ROOT/lib/providers"
  local cl_script="$scripts_dir/cl_${model}.sh"
  local err_log="${out_file}.err.log"

  # Wrapper-wins precedence: a cl_<model>.sh file always takes priority.
  # The providers.yaml registry only handles model names with no wrapper,
  # so BYO entries can never regress the committed providers.
  local _registry_kind=""
  if [[ ! -f "$cl_script" ]]; then
    if declare -f mo_provider_kind >/dev/null 2>&1; then
      _registry_kind=$(mo_provider_kind "$model" 2>/dev/null) || _registry_kind=""
    fi
    if [[ -z "$_registry_kind" ]]; then
      echo "mo_llm_dispatch: cl_${model}.sh missing at $cl_script and no providers.yaml entry" >> "$err_log"
      return 2
    fi
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
    if declare -f mo_emit_cache_flags >/dev/null 2>&1 && ! _mo_llm_is_gateway "$model"; then
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

  # v0.2-pt38 (E-MO-19, 2026-06-02): gateway-detection bypass for stream-json.
  # Observed 2026-06-01 perf report: MO_TRACE_RICH=1 + cl_minimax/cl_glm
  # hangs to SIGTERM @ 90s because gateway endpoints (api.minimax.io,
  # api.z.ai for GLM) don't stream `stream-json` events the way native
  # Anthropic does — client waits for type=result line that never arrives.
  # Force-fallback to `json` mode for known-gateway models. Native
  # Anthropic (opus/sonnet/opus_oauth) keeps rich-trace capture.
  # Override: MO_FORCE_STREAM_JSON_ON_GATEWAY=1 keeps stream-json on for
  # gateways (e.g. when testing a fixed gateway).
  if [ "$_format" = "stream-json" ] && \
     [ "${MO_FORCE_STREAM_JSON_ON_GATEWAY:-0}" != "1" ] && \
     _mo_llm_is_gateway "$model"; then
    _format="json"
    # Stash the override reason in err_log later (after err_log is defined)
    : "$model is a gateway model — downgrading to json output to avoid stream-json hang"
  fi
  local _raw_out="${out_file}.raw"

  # Secrets are sourced inside each dispatch subshell so api_key_env vars
  # declared in providers.yaml resolve for both branch kinds.
  local secrets="${MINI_ORK_SECRETS:-${MINI_ORK_HOME:-.mini-ork}/config/secrets.local.sh}"

  if _mo_llm_is_executable "$model" || [[ "$_registry_kind" == "openai-compat" || "$_registry_kind" == "executable" ]]; then
    # Executable wrapper: cl_codex.sh / cl_gemini.sh handle their own CLI
    # (these don't support --output-format json universally → keep text)
    local _exec_bin="$cl_script"
    local _exec_env=()
    if [[ ! -f "$cl_script" ]]; then
      case "$_registry_kind" in
        openai-compat)
          # Route through cl_codex.sh with the BYO endpoint contract
          _exec_bin="$scripts_dir/cl_codex.sh"
          local _oai_model _oai_base _oai_key_env
          _oai_model=$(mo_provider_field "$model" model 2>/dev/null) || _oai_model=""
          _oai_base=$(mo_provider_field "$model" base_url 2>/dev/null) || _oai_base=""
          _oai_key_env=$(mo_provider_field "$model" api_key_env 2>/dev/null) || _oai_key_env=""
          [[ -n "$_oai_model" ]] && _exec_env+=("MO_OAI_MODEL=$_oai_model")
          [[ -n "$_oai_base" ]] && _exec_env+=("MO_OAI_BASE_URL=$_oai_base")
          [[ -n "$_oai_key_env" ]] && _exec_env+=("MO_OAI_ENV_KEY=$_oai_key_env")
          ;;
        executable)
          local _reg_script
          _reg_script=$(mo_provider_field "$model" script 2>/dev/null) || _reg_script=""
          if [[ -z "$_reg_script" ]]; then
            echo "mo_llm_dispatch: provider '$model' kind=executable needs a script field" >> "$err_log"
            return 2
          fi
          [[ "$_reg_script" != /* ]] && _reg_script="$MINI_ORK_ROOT/$_reg_script"
          if [[ ! -x "$_reg_script" ]]; then
            echo "mo_llm_dispatch: provider '$model' script not executable: $_reg_script" >> "$err_log"
            return 2
          fi
          _exec_bin="$_reg_script"
          ;;
      esac
    fi
    # Sidecar contract: wrappers that can harvest token usage (cl_codex.sh
    # parses codex --json turn.completed events) write the same sidecar
    # shapes the claude stream-json path produces, so the envelope/per-turn
    # ledger emission below works identically for executable lanes.
    if [[ -n "$TIMEOUT_CMD" ]]; then
      (
        set +u
        [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
        for _kv in "${_exec_env[@]}"; do export "$_kv"; done
        export MO_USAGE_FILE="${out_file}.tokens" MO_TURNS_FILE="${out_file}.turns.jsonl" \
               MO_COST_FILE="${out_file}.cost"
        "$TIMEOUT_CMD" --foreground --kill-after=60 "$timeout_s" \
          "$_exec_bin" --print --output-format text "$prompt"
      ) > "$out_file" 2>"$err_log" || return $?
    else
      (
        set +u
        [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
        for _kv in "${_exec_env[@]}"; do export "$_kv"; done
        export MO_USAGE_FILE="${out_file}.tokens" MO_TURNS_FILE="${out_file}.turns.jsonl" \
               MO_COST_FILE="${out_file}.cost"
        "$_exec_bin" --print --output-format text "$prompt"
      ) > "$out_file" 2>"$err_log" || return $?
    fi
    _mo_llm_strip_protocol_blocks "$out_file"
    _mo_llm_write_exec_transcript "$out_file" "$model"
  else
    # Sourceable env-export: must run claude in subshell with cl_*.sh sourced

    # v0.2-pt23: stream-json mode requires --verbose per claude CLI contract.
    local _verbose_flag=()
    [ "$_format" = "stream-json" ] && _verbose_flag=(--verbose)

    if [[ -n "$TIMEOUT_CMD" ]]; then
      (
        set +u  # secrets file may reference unset vars
        [[ -f "$secrets" ]] && source "$secrets" 2>/dev/null || true
        if [[ -n "$_registry_kind" ]]; then
          _mo_registry_apply_env "$model" || exit 9
        else
          source "$cl_script"
        fi
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
        if [[ -n "$_registry_kind" ]]; then
          _mo_registry_apply_env "$model" || exit 9
        else
          source "$cl_script"
        fi
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
turns = []  # per-assistant-message usage; one row per real API turn
total_input_tokens = 0
total_output_tokens = 0
total_cache_read_input_tokens = 0
total_cache_creation_input_tokens = 0
# Set True when totals get sourced from the result envelope's usage block
# (the only field the CLI populates accurately). Default False = totals
# are summed from possibly-stub per-turn values and should be treated as
# advisory until the result event lands.
usage_authoritative = False
transcript_fallback = None
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
                # The CLI's per-turn assistant events emit STUB usage values
                # (often input_tokens=2 / output_tokens=41 repeated across every
                # turn with stop_reason=null). The 'result' envelope's usage is
                # the only authoritative count — it's what the API actually
                # billed. ALWAYS take it as ground truth, not only when turns is
                # empty. Per-turn stub values stay in the transcript as advisory
                # (with usage_authoritative flagged below), but the run-level
                # total_input_tokens / total_output_tokens reflect the truth.
                u = obj.get('usage') or {}
                if u:
                    total_input_tokens = int(u.get('input_tokens') or 0)
                    total_output_tokens = int(u.get('output_tokens') or 0)
                    total_cache_read_input_tokens = int(u.get('cache_read_input_tokens') or 0)
                    total_cache_creation_input_tokens = int(u.get('cache_creation_input_tokens') or 0)
                    usage_authoritative = True
            elif et == 'system' and obj.get('subtype') == 'init':
                session_id = obj.get('session_id', session_id)
            elif et == 'assistant':
                msg = obj.get('message', {}) or {}
                # Per-turn usage — one llm_calls row will be written per turn
                u = msg.get('usage') or {}
                turn_in = int(u.get('input_tokens') or 0)
                turn_out = int(u.get('output_tokens') or 0)
                cache_in = int(u.get('cache_read_input_tokens') or 0)
                cache_create = int(u.get('cache_creation_input_tokens') or 0)
                model_id_turn = msg.get('model')
                stop_reason = msg.get('stop_reason')
                # Extract full per-turn text + tool_use blocks for the UI's
                # "agent transcript" view. Without these, the UI shows only
                # tokens/cost metadata — users can't see what the agent
                # actually said or did.
                turn_text_blocks = []
                turn_tool_uses = []
                for block in msg.get('content', []) or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get('type')
                    if btype == 'text':
                        turn_text_blocks.append(block.get('text', ''))
                    elif btype == 'tool_use':
                        turn_tool_uses.append({
                            'id': block.get('id'),
                            'name': block.get('name'),
                            'input': block.get('input', {}),
                        })
                if turn_in or turn_out:
                    # The CLI emits one assistant event PER CONTENT BLOCK
                    # (thinking / text / tool_use), each repeating the same
                    # message id and cumulative usage. Merge events sharing a
                    # message id into one turn, replacing (not summing) usage.
                    msg_id = msg.get('id')
                    prev = turns[-1] if turns else None
                    if prev is not None and msg_id and prev.get('message_id') == msg_id:
                        total_input_tokens += turn_in - prev['input_tokens']
                        total_output_tokens += turn_out - prev['output_tokens']
                        prev['input_tokens'] = turn_in
                        prev['output_tokens'] = turn_out
                        prev['cache_read_input_tokens'] = cache_in
                        prev['cache_creation_input_tokens'] = cache_create
                        if turn_text_blocks:
                            joined = '\n'.join(turn_text_blocks)
                            prev['text'] = (prev['text'] + '\n' + joined) if prev['text'] else joined
                        prev['tool_uses'].extend(turn_tool_uses)
                        if model_id_turn:
                            prev['model'] = model_id_turn
                        if stop_reason:
                            prev['stop_reason'] = stop_reason
                    else:
                        turns.append({
                            'turn_index': len(turns),
                            'model': model_id_turn,
                            'message_id': msg_id,
                            'input_tokens': turn_in,
                            'output_tokens': turn_out,
                            'text': '\n'.join(turn_text_blocks),
                            'tool_uses': turn_tool_uses,
                            'cache_read_input_tokens': cache_in,
                            'cache_creation_input_tokens': cache_create,
                            'stop_reason': stop_reason,
                            'session_id': session_id,
                        })
                        total_input_tokens += turn_in
                        total_output_tokens += turn_out
                # Tool calls (separate concern from token counting)
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

# Strip operator-session protocol blocks (z-insight) that the spawned CLI
# inherited from the operator's global agent config — same sanitizer as
# _mo_llm_strip_protocol_blocks, applied before any consumer sees the text.
import re as _re
def _strip_protocol(s):
    if not s or '<z-insight>' not in s:
        return s
    s = _re.sub(r'<z-insight>.*?</z-insight>', '', s, flags=_re.S)
    s = _re.sub(r'<z-insight>.*\Z', '', s, flags=_re.S)
    return s.rstrip()

result_text = _strip_protocol(result_text)
for _t in turns:
    if _t.get('text'):
        _t['text'] = _strip_protocol(_t['text'])

with open(out_path, 'w') as f:
    f.write((result_text or '') + ('\n' if result_text and not result_text.endswith('\n') else ''))
with open(out_path + '.cost', 'w') as f:
    f.write(f"{total_cost_usd}\n")
# Token totals sidecar (TAB-separated: input\toutput)
with open(out_path + '.tokens', 'w') as f:
    f.write(f"{total_input_tokens}\t{total_output_tokens}\t{total_cache_read_input_tokens}\t{total_cache_creation_input_tokens}\n")
# Per-turn telemetry — one JSONL line per assistant message with usage.
# Consumed by lib/llm-dispatch.sh:llm_dispatch shim to emit one llm_calls row
# per real API turn instead of one summary per agent envelope.
with open(out_path + '.turns.jsonl', 'w') as f:
    for t in turns:
        f.write(json.dumps(t) + '\n')
with open(out_path + '.tool-summary', 'w') as f:
    json.dump({
        'session_id': session_id,
        'tool_calls': tool_calls,
        'files_read': files_read,
        'files_written': files_written,
    }, f)
# Per-turn transcript with FULL content (text + tool_use blocks). The UI's
# AgentDetailPage reads this to render expandable turn cards showing what
# the agent actually said and which tools it called — the user-facing
# "agent transcript" surface. Capped to MAX_TRANSCRIPT_BYTES (1 MiB total
# for the whole turns array) to avoid runaway disk usage on very long runs.
import os as _os
MAX_TRANSCRIPT_BYTES = int(_os.environ.get('MO_MAX_TRANSCRIPT_BYTES', '1048576'))
if not turns and result_text:
    turns.append({
        'turn_index': 0,
        'model': None,
        'input_tokens': total_input_tokens,
        'output_tokens': total_output_tokens,
        'text': result_text,
        'tool_uses': [],
        'cache_read_input_tokens': total_cache_read_input_tokens,
        'cache_creation_input_tokens': total_cache_creation_input_tokens,
        'stop_reason': None,
        'session_id': session_id,
    })
    transcript_fallback = 'text-output'
_payload_obj = {
    'turns': turns,
    'totals': {
        'input_tokens': total_input_tokens,
        'output_tokens': total_output_tokens,
        'cost_usd': total_cost_usd,
    },
    'usage_authoritative': usage_authoritative,
}
if transcript_fallback:
    _payload_obj['fallback'] = transcript_fallback
_payload = json.dumps(_payload_obj)
if len(_payload) > MAX_TRANSCRIPT_BYTES:
    # Trim each turn's text from the END until under cap. Preserve metadata.
    for t in turns:
        if 'text' in t and t['text']:
            t['text'] = t['text'][: max(200, len(t['text']) // 4)] + '\n…[truncated]'
    _payload_obj = {
        'turns': turns,
        'truncated': True,
        'totals': {
            'input_tokens': total_input_tokens,
            'output_tokens': total_output_tokens,
            'cost_usd': total_cost_usd,
        },
        'usage_authoritative': usage_authoritative,
    }
    if transcript_fallback:
        _payload_obj['fallback'] = transcript_fallback
    _payload = json.dumps(_payload_obj)
with open(out_path + '.transcript.json', 'w') as f:
    f.write(_payload)
PY
      local _post_rc=$?
      if [ $_post_rc -ne 0 ]; then
        rm -f "$_raw_out"
        return $_post_rc
      fi
      # Preserve the stream-json log as .stream.jsonl so the UI can offer a
      # "raw stream" download for deep forensics. Per-turn structured content
      # is in .transcript.json (smaller, parsed). The .stream.jsonl is the
      # full unprocessed record. Capped via MO_KEEP_STREAM_JSONL=0 to disable.
      if [ "${MO_KEEP_STREAM_JSONL:-1}" = "1" ]; then
        mv "$_raw_out" "${out_file}.stream.jsonl" 2>/dev/null || rm -f "$_raw_out"
      else
        rm -f "$_raw_out"
      fi
      _mo_llm_persist_agent_transcript "$out_file" "$model"
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
      # Token totals from the JSON envelope's usage block (Anthropic CLI emits
      # them on the top-level result object in non-streaming mode).
      jq -r '"\(.usage.input_tokens // 0)\t\(.usage.output_tokens // 0)\t\(.usage.cache_read_input_tokens // 0)\t\(.usage.cache_creation_input_tokens // 0)"' \
        "$_raw_out" > "${out_file}.tokens" 2>/dev/null || true
      rm -f "$_raw_out"
      _mo_llm_strip_protocol_blocks "$out_file"
      _mo_llm_write_text_transcript "$out_file" "$model"
    else
      mv "$_raw_out" "$out_file"
      _mo_llm_strip_protocol_blocks "$out_file"
      _mo_llm_write_text_transcript "$out_file" "$model"
    fi
  fi
  _mo_llm_persist_agent_transcript "$out_file" "$model"
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

# Generic lane-agnostic retry predicate. Composes the existing classifier
# (capacity|network|stream|provider → retryable; quota|auth|config|request|safety → terminal)
# with an attempt-bound guard. Returns 0 only when retryable category AND
# attempt < max_attempts. Lane-specific overrides (GLM fair-usage) live in their
# own predicates and must be evaluated first in the retry loop.
_mo_llm_throttle_retryable() {
  local model="${1:-}" message="${2:-}" rc="${3:-}" attempt="${4:-1}" max_attempts="${5:-1}"
  [ -n "$model" ] || return 1
  case "$attempt" in ''|*[!0-9]*) attempt=1 ;; esac
  case "$max_attempts" in ''|*[!0-9]*) max_attempts=1 ;; esac
  [ "$attempt" -lt "$max_attempts" ] || return 1
  local _category
  _category=$(_mo_llm_classify_error "$message" "$rc")
  [ "$(_mo_llm_error_retryable "$_category")" = "1" ] || return 1
  return 0
}

_mo_llm_glm_fair_usage_retryable() {
  local model="${1:-}" message="${2:-}" attempt="${3:-1}" max_attempts="${4:-1}"
  [ "$model" = "glm" ] || return 1
  [ "$attempt" -lt "$max_attempts" ] || return 1
  printf '%s' "$message" | grep -Eiq '(^|[^0-9])(429|1313)([^0-9]|$)|fair usage policy|usage pattern does not comply' || return 1
  return 0
}

_mo_llm_glm_backoff_seconds() {
  local attempt="${1:-1}"
  local max_sleep="${MO_GLM_RETRY_MAX_SLEEP_S:-${MO_DISPATCH_RETRY_MAX_SLEEP_S:-45}}"
  local base_s="${MO_GLM_RETRY_BASE_S:-${MO_DISPATCH_RETRY_BASE_S:-5}}"
  _mo_llm_backoff_seconds_raw "$attempt" "$max_sleep" "$base_s"
}

# Generic lane-agnostic backoff: exponential + jitter, capped at max_sleep,
# floored at 1s. attempt clamped to [1,12] to avoid bash integer overflow
# (`2**(attempt-1)` exceeds 64-bit past attempt≈31).
_mo_llm_backoff_seconds() {
  local attempt="${1:-1}" max_sleep="${MO_DISPATCH_RETRY_MAX_SLEEP_S:-45}" base_s="${MO_DISPATCH_RETRY_BASE_S:-5}"
  _mo_llm_backoff_seconds_raw "$attempt" "$max_sleep" "$base_s"
}

_mo_llm_backoff_seconds_raw() {
  local attempt="$1" max_sleep="$2" base_s="$3"
  case "$max_sleep" in ''|*[!0-9]*) max_sleep=45 ;; esac
  case "$base_s" in ''|*[!0-9]*) base_s=5 ;; esac
  case "$attempt" in ''|*[!0-9]*) attempt=1 ;; esac
  [ "$attempt" -lt 1 ] && attempt=1
  [ "$attempt" -gt 12 ] && attempt=12
  local base=$((2 ** (attempt - 1) * base_s))
  local jitter=$((RANDOM % 4))
  local delay=$((base + jitter))
  [ "$delay" -gt "$max_sleep" ] && delay="$max_sleep"
  [ "$delay" -lt 1 ] && delay=1
  printf '%s\n' "$delay"
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
  # Node wall-clock + turn caps are env-overridable. Slow lanes (e.g. kimi at
  # ~30s/turn) doing many grep/read tool calls can exceed the 1500s default and
  # get SIGKILLed mid-work — claude --print buffers stdout until exit, so the
  # kill produces a 0-byte artifact, which then cascade-skips dependents.
  local _timeout_s="${MO_NODE_TIMEOUT_S:-1500}" _max_turns="${MO_NODE_MAX_TURNS:-60}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task-class)  task_class="$2";     shift 2 ;;
      --node-type)   node_type="$2";      shift 2 ;;
      --prompt-text) prompt_text="$2";    shift 2 ;;
      --out)         out_file="$2";       shift 2 ;;
      --model)       model_override="$2"; shift 2 ;;
      --timeout)     _timeout_s="$2";     shift 2 ;;
      --max-turns)   _max_turns="$2";     shift 2 ;;
      *)             shift ;;
    esac
  done

  # v0.2-pt7 (R10): cost circuit breaker. Check accumulated daily spend
  # against the daily cap before dispatching. The cap is single-sourced from
  # config/agents.yaml (budget.daily_cap_usd) via mo_daily_budget_cap, with
  # MO_DAILY_BUDGET_USD overriding and a 50 fallback when config is absent.
  # Returns non-zero with `[cost_circuit_open]` marker if exceeded so the
  # caller's $? check trips, halting the run gracefully.
  if [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ]; then
    local _budget
    if declare -F mo_daily_budget_cap >/dev/null 2>&1; then
      _budget="$(mo_daily_budget_cap)"
    else
      _budget="${MO_DAILY_BUDGET_USD:-50}"
    fi
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

	  if [ "${MO_FUSE_ENABLED:-0}" = "1" ] && [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ]; then
	    local _category
	    for _category in capacity network stream provider; do
	      if ! _mo_check_lane_fuse "$node_type" "$_category" >/dev/null; then
	        _mo_record_lane_fuse_trip "$node_type" "$_category"
	        echo "[lane_fuse_open] lane=$node_type error_category=$_category consecutive_failures=3 — halting dispatch" >&2
	        return 43
	      fi
	    done
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
  local _duration_start_ms _duration_end_ms _duration_ms
  _duration_start_ms=$(_mo_llm_now_ms) || {
    _mo_llm_write_duration_ms 0
    return 127
  }
  local _dispatch_ok=0 _dispatch_rc=0 _attempt=1 _max_attempts="${MO_DISPATCH_MAX_ATTEMPTS:-3}"
  case "$_max_attempts" in ''|*[!0-9]*) _max_attempts=3 ;; esac
  [ "$_max_attempts" -lt 1 ] && _max_attempts=1
  if [ "$model" = "glm" ] && [ -n "${MO_GLM_MAX_ATTEMPTS:-}" ]; then
    case "$MO_GLM_MAX_ATTEMPTS" in ''|*[!0-9]*) ;; *) [ "$MO_GLM_MAX_ATTEMPTS" -gt "$_max_attempts" ] && _max_attempts="$MO_GLM_MAX_ATTEMPTS" ;; esac
  fi
  while :; do
    : > "$_err_file" 2>/dev/null || true
    rm -f "${out_file}.err.log" 2>/dev/null || true
    # Capture rc into a var IMMEDIATELY — `if cmd; then…; fi` with no else
    # returns 0 when cmd fails, so reading $? after `fi` would mask the real
    # dispatch rc (D-013/D-014 regression: shim reported rc=0 on a hard fail).
    mo_llm_dispatch "$model" "$prompt_text" "$out_file" "$_timeout_s" "$_max_turns" >/dev/null 2>"$_err_file"
    _dispatch_rc=$?
    if [ "$_dispatch_rc" -eq 0 ]; then
      _dispatch_ok=1
      break
    fi
    local _retry_probe=""
    _retry_probe=$(
      {
        tail -c 1000 "$_err_file" 2>/dev/null || true
        tail -c 1000 "${out_file}.err.log" 2>/dev/null || true
        tail -c 1000 "$out_file" 2>/dev/null || true
      } | tail -c 2000
    )
    local _category
    _category=$(_mo_llm_classify_error "$_retry_probe" "$_dispatch_rc")
    # GLM fair-usage must be evaluated FIRST: its raw 429/1313 regex catches
    # 'fair usage' messages that the generic classifier routes to 'quota'
    # (= non-retryable). Falling through to the generic predicate would lose
    # GLM retries and silently sink runs (the original failure mode).
    if _mo_llm_glm_fair_usage_retryable "$model" "$_retry_probe" "$_attempt" "$_max_attempts" \
       || _mo_llm_throttle_retryable "$model" "$_retry_probe" "$_dispatch_rc" "$_attempt" "$_max_attempts"; then
      local _sleep_s _reason
      if _mo_llm_glm_fair_usage_retryable "$model" "$_retry_probe" "$_attempt" "$_max_attempts"; then
        _reason="fair_usage"
      else
        _reason="${_category:-unknown}"
      fi
      _sleep_s=$(_mo_llm_backoff_seconds "$_attempt")
      echo "[llm_dispatch RETRY model=$model reason=${_reason} attempt=${_attempt}/${_max_attempts} sleep=${_sleep_s}s]" >&2
      sleep "$_sleep_s"
      _attempt=$((_attempt + 1))
      continue
    fi
    break
  done
  if [ "$_dispatch_ok" -eq 1 ]; then
    _duration_end_ms=$(_mo_llm_now_ms) || {
      _mo_llm_write_duration_ms 0
      return 127
    }
    _duration_ms=$((_duration_end_ms - _duration_start_ms))
    [ "$_duration_ms" -lt 0 ] && _duration_ms=0
    _mo_llm_write_duration_ms "$_duration_ms"
    _mo_llm_persist_agent_transcript "$out_file" "$model"
    cat "$out_file"
    local _cost_usd="0"
    if [ -f "${out_file}.cost" ]; then
      _cost_usd=$(cat "${out_file}.cost" 2>/dev/null || printf '0')
    fi
    # Token totals from sidecar (claude --output-format json emits .usage)
    local _in_tok=0 _out_tok=0 _cached_in_tok=0 _cache_create_tok=0
    if [ -f "${out_file}.tokens" ]; then
      IFS=$'\t' read -r _in_tok _out_tok _cached_in_tok _cache_create_tok < "${out_file}.tokens" 2>/dev/null || true
      _in_tok="${_in_tok:-0}"; _out_tok="${_out_tok:-0}"
      _cached_in_tok="${_cached_in_tok:-0}"; _cache_create_tok="${_cache_create_tok:-0}"
    fi

    # Per-turn emission: when stream-json captured per-assistant-message usage,
    # write ONE llm_calls row per real API turn. This is the "full transparency
    # on agent runs" surface — instead of a single envelope row per agent
    # invocation, each underlying claude API call is visible. Falls back to a
    # single summary row when turns sidecar is absent (text/json modes,
    # codex/gemini executable lanes).
    local _provider; _provider=$(_mo_llm_provider_for_model "$model")
    local _feature="mini-ork:${node_type:-unknown}"
    local _actor="${MO_LANE_ACTOR:-${node_type:-${USER:-unknown}}}"
    if [ -f "${out_file}.turns.jsonl" ] && [ -s "${out_file}.turns.jsonl" ]; then
      # Read each turn → emit one row. Cost is split proportionally across turns.
      # Per-turn emit path: pass MINI_ORK_TASK_RUN_ID too so the python block
      # can auto-derive traceparent when MO_TRACEPARENT is empty. Without
      # this fallback every per-turn row landed with traceparent=NULL and
      # the UI's strict-bridge filter dropped them.
      MO_NODE_ID="${MO_NODE_ID:-}" \
      python3 - "${out_file}.turns.jsonl" "$_cost_usd" "$_provider" "$_feature" \
                "$_actor" "$model" "$_duration_ms" \
                "${MO_LANE_TIER:-default}" "${MINI_ORK_DB:-}" \
                "${MO_RECURSIVE_ITER:-}" "${MINI_ORK_RUN_ID:-}" \
                "${MO_TRACEPARENT:-}" "${MINI_ORK_TASK_RUN_ID:-}" <<'PY' 2>/dev/null || true
import json, os, secrets, sqlite3, sys
turns_path, cost_total, provider, feature, actor, model, duration_ms, \
  tier, db, iter_, run_id, traceparent, task_run_id = sys.argv[1:14]

# Auto-derive traceparent if env was empty — matches _mo_llm_write_llm_calls_row's
# fallback so both emit paths produce identical traceparent shape.
if not traceparent and task_run_id and db:
    try:
        _con = sqlite3.connect(db, timeout=2.0)
        _row = _con.execute(
            "SELECT COALESCE(trace_id,'') FROM task_runs WHERE id=? LIMIT 1",
            (task_run_id,),
        ).fetchone()
        _con.close()
        _tid = _row[0] if _row else ""
        if _tid:
            traceparent = f"00-{_tid}-{secrets.token_hex(8)}-01"
    except Exception:
        pass
if not db: sys.exit(0)
try:
    turns = [json.loads(line) for line in open(turns_path) if line.strip()]
except Exception:
    sys.exit(0)
if not turns: sys.exit(0)
total_out = sum(int(t.get('output_tokens') or 0) for t in turns) or 1
cost_total_f = float(cost_total or 0.0)
con = sqlite3.connect(db, timeout=5)
con.execute("PRAGMA busy_timeout=5000")
cols = {r[1] for r in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
for t in turns:
    # Cost split proportionally by output_tokens (output dominates cost)
    out_tok = int(t.get('output_tokens') or 0)
    in_tok = int(t.get('input_tokens') or 0)
    cached_in = int(t.get('cache_read_input_tokens') or 0)
    cache_create = int(t.get('cache_creation_input_tokens') or 0)
    uncached_in = max(in_tok - cached_in - cache_create, 0)
    cost_input_uncached = uncached_in * 15.0 / 1_000_000
    cost_input_cached = cached_in * 1.5 / 1_000_000
    cost_cache_write = cache_create * 18.75 / 1_000_000
    share = (out_tok / total_out) if total_out else 0
    cost_share = cost_total_f * share
    meta = json.dumps({
        'turn_index': t.get('turn_index'),
        'session_id': t.get('session_id'),
        'stop_reason': t.get('stop_reason'),
        'cache_read_input_tokens': t.get('cache_read_input_tokens', 0),
        'cache_creation_input_tokens': t.get('cache_creation_input_tokens', 0),
        # MO_NODE_ID is the canonical recipe node name (e.g. perf_lens).
        # Falls back to feature_name suffix (lane/family) only when env
        # var isn't set — that path is ambiguous when multiple nodes
        # share a lane (the 4 lens nodes all use lens lanes).
        'node_id': os.environ.get('MO_NODE_ID') or (feature.split(':',1)[1] if ':' in feature else None),
    })
    insert_cols = [
        "provider", "model_id", "tier", "feature_name", "actor",
        "status", "duration_ms", "cost_usd", "error_message", "iter",
        "run_id", "traceparent", "input_tokens", "output_tokens",
        "total_tokens", "metadata_json", "session_id",
    ]
    values = [
        provider, t.get('model') or model, tier, feature, actor or None,
        'success',
        # Per-turn duration unknown from claude stream-json; spread evenly.
        int(int(duration_ms or 0) / max(len(turns), 1)),
        cost_share, None,
        int(iter_) if iter_ else None,
        run_id or None,
        traceparent or None,
        in_tok, out_tok, in_tok + out_tok,
        meta,
        t.get('session_id'),
    ]
    if "error_category" in cols:
        insert_cols.append("error_category")
        values.append(None)
    if "retryable" in cols:
        insert_cols.append("retryable")
        values.append(None)
    if "cached_input_tokens" in cols:
        insert_cols.append("cached_input_tokens")
        values.append(cached_in)
    if "cache_creation_input_tokens" in cols:
        insert_cols.append("cache_creation_input_tokens")
        values.append(cache_create)
    if "cost_input_uncached_usd" in cols:
        insert_cols.append("cost_input_uncached_usd")
        values.append(cost_input_uncached)
    if "cost_input_cached_usd" in cols:
        insert_cols.append("cost_input_cached_usd")
        values.append(cost_input_cached)
    if "cost_cache_write_usd" in cols:
        insert_cols.append("cost_cache_write_usd")
        values.append(cost_cache_write)
    placeholders = ",".join("?" for _ in insert_cols)
    con.execute(
        f"INSERT INTO llm_calls ({', '.join(insert_cols)}) VALUES ({placeholders})",
        values,
    )
con.commit()
con.close()
PY
    else
      _mo_llm_write_llm_calls_row \
        "$_provider" "$model" "${MO_LANE_TIER:-default}" \
        "$_feature" "$_actor" \
        "success" "$_duration_ms" "$_cost_usd" "" \
        "$_in_tok" "$_out_tok" "{}" "$_cached_in_tok" "$_cache_create_tok"
    fi
    rm -f "${out_file}.tokens" "${out_file}.turns.jsonl" 2>/dev/null || true
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
    local rc="$_dispatch_rc"
    export MO_LLM_LAST_RC="$rc"
    _mo_llm_write_duration_ms 0
    local _error_message=""
    _error_message=$(
      {
        tail -c 200 "$_err_file" 2>/dev/null || true
        tail -c 200 "${out_file}.err.log" 2>/dev/null || true
      } | tail -c 400
    )
    _error_message=$(_mo_llm_redact_secrets "$_error_message")
    _mo_llm_write_llm_calls_row \
      "$(_mo_llm_provider_for_model "$model")" "$model" "${MO_LANE_TIER:-default}" \
      "mini-ork:${node_type:-unknown}" "${MO_LANE_ACTOR:-${node_type:-${USER:-unknown}}}" \
      "failed" "0" "0" "$_error_message" "0" "0" "{}"
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
