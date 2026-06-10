#!/usr/bin/env bash
# cl_codex.sh — executable wrapper that adapts mini-ork's dispatcher
# calling convention to the OpenAI Codex CLI shape.
#
# Why executable (not sourceable like cl_glm.sh / cl_opus.sh):
# Codex isn't an Anthropic-compatible gateway — it's a separate CLI
# with its own auth (~/.codex/config.toml) and its own invocation
# pattern (`codex exec [PROMPT]`). The framework's lib/llm-dispatch.sh
# dispatcher has TWO branches at mo_llm_dispatch:
#
#   - Sourceable (cl_glm/cl_kimi/cl_opus/cl_sonnet/cl_deepseek/cl_minimax):
#     source the cl_*.sh in a subshell → env-vars pin claude's
#     ANTHROPIC_BASE_URL/MODEL → invoke `claude --print --output-format
#     text "$prompt"`.
#
#   - Executable (cl_codex / cl_gemini per _MO_LLM_EXECUTABLE_MODELS in
#     lib/llm-dispatch.sh:54): call the cl_*.sh as a binary with the
#     same `--print --output-format text "$prompt"` args; wrapper
#     translates to native CLI shape.
#
# This wrapper IS the executable form for codex. It accepts the
# dispatcher's flag dialect and emits the same shape of output (raw
# text or JSON envelope) to stdout.
#
# Requires: `codex` CLI on PATH + ~/.codex/config.toml authenticated
# (run `codex login` if not).
#
# v0.2-pt15 (D-049): closes the cl_codex.sh gap that made positioning
# claim "codex_lens → codex" undeliverable. Validates by `mini-ork run
# refactor-audit` or `research-synthesis` actually routing codex_lens
# through this wrapper.

set -uo pipefail

# Parse the dispatcher contract:
#   cl_codex.sh --print --output-format text "$prompt"
#   cl_codex.sh --print --output-format json "$prompt"
FORMAT="text"
PROMPT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --print)               shift ;;        # accept + ignore (claude compat)
    --output-format)       FORMAT="$2"; shift 2 ;;
    --permission-mode)     shift 2 ;;      # accept + ignore (claude compat)
    --max-turns)           shift 2 ;;      # accept + ignore (claude compat)
    --exclude-dynamic-system-prompt-sections) shift ;;  # cache flag — ignore
    -*)                    shift ;;        # any other flag — ignore (don't fail)
    *)
      if [ -z "$PROMPT" ]; then
        PROMPT="$1"
      fi
      shift
      ;;
  esac
done

if [ -z "$PROMPT" ]; then
  echo "[cl_codex] no prompt provided" >&2
  exit 2
fi

# Check codex CLI presence + auth
if ! command -v codex >/dev/null 2>&1; then
  echo "[cl_codex] codex CLI not found on PATH — install via https://github.com/openai/codex" >&2
  exit 3
fi

# BYO OpenAI-compatible endpoint (providers.yaml registry contract).
# When lib/llm-dispatch.sh routes an `openai-compat` registry entry through
# this wrapper it exports:
#   MO_OAI_MODEL    → model id to request (`-m`)
#   MO_OAI_BASE_URL → OpenAI-compatible /v1 endpoint
#   MO_OAI_ENV_KEY  → NAME of the env var holding the API key (codex reads
#                     it itself via model_providers.<id>.env_key — the key
#                     value never appears on the command line)
# Without these vars the wrapper keeps its default behavior: ambient
# `codex login` auth + the operator's ~/.codex/config.toml model.
_CODEX_BYO_FLAGS=()
if [ -n "${MO_OAI_BASE_URL:-}" ] && [ -n "${MO_OAI_ENV_KEY:-}" ]; then
  if [ -z "${!MO_OAI_ENV_KEY:-}" ]; then
    echo "[cl_codex] \$$MO_OAI_ENV_KEY is empty — set it in secrets.local.sh or the environment" >&2
    exit 5
  fi
  _CODEX_BYO_FLAGS+=(
    -c "model_providers.mini_ork={ name = \"mini-ork BYO\", base_url = \"$MO_OAI_BASE_URL\", env_key = \"$MO_OAI_ENV_KEY\", wire_api = \"chat\" }"
    -c "model_provider=mini_ork"
  )
fi
if [ -n "${MO_OAI_MODEL:-}" ]; then
  _CODEX_BYO_FLAGS+=(-m "$MO_OAI_MODEL")
fi

# Invoke codex exec. The `--skip-git-repo-check` flag avoids the prompt
# that codex emits when not in a git repo; we may run from /tmp / .mini-ork/runs/.
# `--json` makes codex emit JSONL events on stdout — turn.completed events
# carry the real token usage (input/cached/output) that the plain transcript
# only shows as a strippable "tokens used:" status line.
# `--output-last-message` gives mini-ork the assistant body instead of the
# terminal transcript (prompt + status + hooks), which would confuse downstream
# JSON extraction. Default to workspace-write because implementer nodes must be
# able to edit the scenario project; operators can override with CODEX_SANDBOX.
_CODEX_LAST_MESSAGE="$(mktemp -t mini-ork-codex-last.XXXXXX)"
_CODEX_SANDBOX="${CODEX_SANDBOX:-workspace-write}"
RAW_OUT=$(codex exec \
  --skip-git-repo-check \
  --sandbox "$_CODEX_SANDBOX" \
  --json \
  --output-last-message "$_CODEX_LAST_MESSAGE" \
  ${_CODEX_BYO_FLAGS[@]+"${_CODEX_BYO_FLAGS[@]}"} \
  "$PROMPT" 2>&1) || {
  echo "[cl_codex] codex exec failed with rc=$? — see stderr for cause" >&2
  echo "$RAW_OUT" >&2
  rm -f "$_CODEX_LAST_MESSAGE"
  exit 4
}

# Harvest usage + per-turn sidecars from the JSONL event stream BEFORE
# RAW_OUT gets replaced by the last-message body. The dispatcher exports:
#   MO_USAGE_FILE → TSV "input_tokens<TAB>output_tokens" (envelope row totals)
#   MO_TURNS_FILE → turns.jsonl lines matching the stream-json per-turn shape
# Both optional — absent env vars skip the sidecar (standalone CLI use).
if [ -n "${MO_USAGE_FILE:-}" ] || [ -n "${MO_TURNS_FILE:-}" ] || [ -n "${MO_COST_FILE:-}" ]; then
  RAW_OUT="$RAW_OUT" python3 - "${MO_USAGE_FILE:-}" "${MO_TURNS_FILE:-}" "${MO_COST_FILE:-}" <<'PY' || true
import json, os, sys
usage_path, turns_path, cost_path = sys.argv[1:4]
in_tok = out_tok = cached_tok = 0
turns = []
thread_id = None
for line in os.environ.get("RAW_OUT", "").splitlines():
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("type") == "thread.started":
        thread_id = ev.get("thread_id")
    if ev.get("type") == "turn.completed":
        u = ev.get("usage") or {}
        t_in = int(u.get("input_tokens") or 0)
        t_out = int(u.get("output_tokens") or 0)
        t_cached = int(u.get("cached_input_tokens") or 0)
        in_tok += t_in
        out_tok += t_out
        cached_tok += t_cached
        turns.append({
            "turn_index": len(turns),
            "input_tokens": t_in,
            "output_tokens": t_out,
            "cache_read_input_tokens": t_cached,
            "model": "codex",
            "session_id": thread_id,
        })
if usage_path and (in_tok or out_tok):
    with open(usage_path, "w") as f:
        f.write(f"{in_tok}\t{out_tok}\n")
if turns_path and turns:
    with open(turns_path, "w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")
# Estimated cost: codex CLI exposes no billing figure on this surface, so
# derive one from token usage at list price. Defaults are OpenAI's published
# gpt-5/codex rates (USD per 1M tokens); override per deployment via env.
# Note: usage.input_tokens INCLUDES cached_input_tokens, which bill at the
# discounted rate — subtract before applying the full input rate.
if cost_path and (in_tok or out_tok):
    p_in = float(os.environ.get("MO_CODEX_USD_PER_MTOK_IN", "1.25"))
    p_cached = float(os.environ.get("MO_CODEX_USD_PER_MTOK_CACHED", "0.125"))
    p_out = float(os.environ.get("MO_CODEX_USD_PER_MTOK_OUT", "10.0"))
    fresh_in = max(in_tok - cached_tok, 0)
    cost = (fresh_in * p_in + cached_tok * p_cached + out_tok * p_out) / 1e6
    with open(cost_path, "w") as f:
        f.write(f"{cost:.6f}\n")
PY
fi

if [ -s "$_CODEX_LAST_MESSAGE" ]; then
  RAW_OUT="$(cat "$_CODEX_LAST_MESSAGE")"
else
  # --json mode: stdout is JSONL, not a transcript. Reconstruct the assistant
  # body from agent_message events so the marker-strip fallback below still
  # has plain text to work with.
  _CODEX_MSG=$(RAW_OUT="$RAW_OUT" python3 - <<'PY' || true
import json, os
msgs = []
for line in os.environ.get("RAW_OUT", "").splitlines():
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    item = ev.get("item") or {}
    if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
        msgs.append(item.get("text") or "")
print("\n\n".join(m for m in msgs if m))
PY
)
  [ -n "$_CODEX_MSG" ] && RAW_OUT="$_CODEX_MSG"
fi
rm -f "$_CODEX_LAST_MESSAGE"

# Strip codex's transcript envelope so downstream parsers see the assistant
# body only. Codex CLI can emit:
#
#   user
#   <full prompt, including JSON examples>
#   codex
#   <assistant answer>
#
# If we pass that whole transcript through, mini-ork-plan's balanced JSON
# extractor sees the prompt's example JSON before the actual answer. Keep text
# after the final bare `codex` marker when present, then remove status lines.
CLEAN=$(RAW_OUT="$RAW_OUT" python3 - <<'PY'
import re, sys
import os
txt = os.environ.get("RAW_OUT", "")
lines = txt.splitlines()
last_codex = -1
for i, line in enumerate(lines):
    if line.strip() == "codex":
        last_codex = i
if last_codex >= 0:
    lines = lines[last_codex + 1:]
drop = (
    re.compile(r"^\[20[0-9]{2}-[0-9]{2}-[0-9]{2}T"),
    re.compile(r"^tokens used:"),
    re.compile(r"^User instructions:"),
    re.compile(r"^OpenAI Codex"),
    re.compile(r"^Reading additional input from stdin"),
    re.compile(r"^[-]{8,}$"),
    re.compile(r"^(workdir|model|provider|approval|sandbox|reasoning|session id):"),
    re.compile(r"^hook: "),
)
kept = []
for line in lines:
    if any(rx.search(line) for rx in drop):
        continue
    kept.append(line)
print("\n".join(kept).strip())
PY
)
[ -z "$CLEAN" ] && CLEAN="$RAW_OUT"

if [ "$FORMAT" = "json" ]; then
  # Emit a minimal claude-shaped JSON envelope so downstream jq parser
  # (lib/llm-dispatch.sh D-04 post-process at lines ~245-255) finds
  # `.result` + `.total_cost_usd`. Codex doesn't expose per-call cost
  # to us via this CLI surface, so total_cost_usd is 0; caller's
  # _d022_charge_node_cost falls back to $0.01 placeholder which is
  # the documented behavior for executable-wrapper lanes.
  python3 -c "
import json, sys
print(json.dumps({
    'result': sys.argv[1],
    'total_cost_usd': 0.0,
    'model': 'codex',
}))
" "$CLEAN"
else
  printf '%s\n' "$CLEAN"
fi
