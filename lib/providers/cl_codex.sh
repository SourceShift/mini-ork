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

# Stdin prompt mode (Phase-0 wiring): when no positional prompt was given and
# stdin is a pipe/file (not a tty), read the prompt from stdin. This lets the
# Python dispatch layer (mini_ork.dispatch) drive the wrapper without ever
# putting the prompt on argv — E2BIG-proof from the caller all the way in.
# Backward-compatible: existing argv callers are unaffected.
if [ -z "$PROMPT" ] && [ ! -t 0 ]; then
  PROMPT="$(cat)"
fi

if [ -z "$PROMPT" ]; then
  echo "[cl_codex] no prompt provided (positional arg or stdin)" >&2
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

# Pin codex's working root explicitly via -C/--cd so it cannot drift to
# MINI_ORK_ROOT (or any other ambient cwd) when the dispatcher subshell
# inherits a path-confused cwd. Diagnosed 2026-06-13: an implementer dispatch
# from a sibling research workspace landed codex with cwd=<mini-ork root>
# (codex session_meta confirmed). The
# implementer then grep'd mini-ork's own README/ROADMAP/recipes instead of
# the target repo's CWT-A kickoff files. Zero target-repo edits, $0.48 burned.
#
# Resolution order:
#   1. MO_TARGET_CWD env (dispatcher sets this when it knows the target repo —
#      e.g. derived from the kickoff_path's git toplevel).
#   2. $PWD as recovered from the subshell — usually correct when the
#      dispatcher's parent shell ran in the target repo.
# Both fall back to "" if neither is set, in which case we skip --cd and
# preserve codex's prior behavior (inherits whatever cwd the process has).
_CODEX_TARGET_CWD="${MO_TARGET_CWD:-${PWD:-}}"
_CODEX_CD_FLAGS=()
if [ -n "$_CODEX_TARGET_CWD" ] && [ -d "$_CODEX_TARGET_CWD" ]; then
  _CODEX_CD_FLAGS+=(-C "$_CODEX_TARGET_CWD")
fi

# Codex may refresh plugins/skills with `git ls-remote` before starting a
# turn. Some developer machines rewrite GitHub HTTPS remotes to SSH, which can
# prompt for an encrypted key passphrase and wedge non-interactive mini-ork
# runs. Keep git network checks fail-fast and non-interactive.
export GIT_TERMINAL_PROMPT="${GIT_TERMINAL_PROMPT:-0}"
export GIT_ASKPASS="${GIT_ASKPASS:-/bin/false}"
export SSH_ASKPASS="${SSH_ASKPASS:-/bin/false}"
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o NumberOfPasswordPrompts=0}"

# Real-time progress sidecar (2026-06-11 fix).
# Problem: `RAW_OUT=$(codex exec ... 2>&1)` buffers ALL stdout until codex
# exits, so a 15-20 min implementer turn writes ZERO lines to mini-ork-execute's
# log until the very end. Observers (Monitor tail / UI / operator) cannot
# distinguish a live codex run from a deadlock — diagnosed 2026-06-11
# run-1781188025-72603 spent 17+ min looking like a hang to a tail-based
# monitor while codex was actually progressing through tool calls.
# Fix: tee codex's JSONL stream to a sidecar so turn.completed / tool_call
# events are visible AS THEY HAPPEN. The sidecar path is derived from
# MO_USAGE_FILE when set (same dir as out-file), else /tmp.
if [ -n "${MO_USAGE_FILE:-}" ]; then
  _CODEX_STREAM_FILE="${MO_USAGE_FILE%.tokens}.stream.jsonl"
else
  _CODEX_STREAM_FILE="$(mktemp -t mini-ork-codex-stream.XXXXXX)"
fi
export MO_CODEX_STREAM_FILE="$_CODEX_STREAM_FILE"

# Run codex with plain file redirects. Avoid FIFO/tee/process-substitution
# chains: on macOS job control can stop or wedge the wrapper before a real
# Codex process appears, leaving mini-ork heartbeats but no agent output.
set +e
{
  printf '[cl_codex] launching codex exec cwd=%s sandbox=%s\n' "$_CODEX_TARGET_CWD" "$_CODEX_SANDBOX"
  printf '[cl_codex] prompt_bytes=%s\n' "$(printf '%s' "$PROMPT" | wc -c | tr -d ' ')"
} >> "$_CODEX_STREAM_FILE" 2>/dev/null || true
codex exec \
  --skip-git-repo-check \
  --sandbox "$_CODEX_SANDBOX" \
  --json \
  --output-last-message "$_CODEX_LAST_MESSAGE" \
  ${_CODEX_CD_FLAGS[@]+"${_CODEX_CD_FLAGS[@]}"} \
  ${_CODEX_BYO_FLAGS[@]+"${_CODEX_BYO_FLAGS[@]}"} \
  -- "$PROMPT" \
  </dev/null >> "$_CODEX_STREAM_FILE" 2>&1
_CODEX_RC=$?
RAW_OUT="$(cat "$_CODEX_STREAM_FILE" 2>/dev/null || true)"
set -uo pipefail

if [ "$_CODEX_RC" -ne 0 ]; then
  echo "[cl_codex] codex exec failed with rc=$_CODEX_RC — see stderr for cause" >&2
  echo "$RAW_OUT" >&2
  rm -f "$_CODEX_LAST_MESSAGE"
  exit 4
fi

# Harvest usage + per-turn sidecars from the JSONL event stream BEFORE
# RAW_OUT gets replaced by the last-message body. The dispatcher exports:
#   MO_USAGE_FILE → TSV "input_tokens<TAB>output_tokens" (envelope row totals)
#   MO_TURNS_FILE → turns.jsonl lines matching the stream-json per-turn shape
# Both optional — absent env vars skip the sidecar (standalone CLI use).
if [ -n "${MO_USAGE_FILE:-}" ] || [ -n "${MO_TURNS_FILE:-}" ] || [ -n "${MO_COST_FILE:-}" ]; then
  # Pass the stream file PATH (short), not its content — exec() counts env
  # + argv toward ARG_MAX, so a multi-KB RAW_OUT in the environment is E2BIG
  # ("Argument list too long"). RAW_OUT here is a verbatim cat of this file.
  MO_RAW_FILE="$_CODEX_STREAM_FILE" python3 - "${MO_USAGE_FILE:-}" "${MO_TURNS_FILE:-}" "${MO_COST_FILE:-}" <<'PY' || true
import json, os, sys
usage_path, turns_path, cost_path = sys.argv[1:4]
in_tok = out_tok = cached_tok = 0
turns = []
thread_id = None
def _read_raw():
    p = os.environ.get("MO_RAW_FILE", "")
    if not p:
        return ""
    try:
        with open(p, "r", errors="replace") as _f:
            return _f.read()
    except OSError:
        return ""
for line in _read_raw().splitlines():
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
# derive one from token usage at list price. Precedence (2026-06-15 fix):
#   1. Env-var override (MO_CODEX_USD_PER_MTOK_*) - operator-set, wins.
#   2. .mini-ork/config/pricing.yaml via lib/pricing_strategy.sh.
#   3. Inline hardcoded default.
# tests/integration/test_dispatch_telemetry_gate.sh injects env-var
# overrides to validate cost math at known rates; if pricing.yaml is
# checked first, those overrides are silently ignored.
# Note: usage.input_tokens INCLUDES cached_input_tokens, which bill at the
# discounted rate — subtract before applying the full input rate.
if cost_path and (in_tok or out_tok):
    import subprocess

    def _rate(model, kind, env_name, hardcoded_default):
        # 1. Env-var override wins (operator-set; tests rely on this).
        env_val = os.environ.get(env_name)
        if env_val:
            try:
                return float(env_val)
            except ValueError:
                pass
        # 2. pricing.yaml lookup via pricing_strategy.sh.
        try:
            rc = subprocess.run(
                ["bash", "-c",
                 "source ${MINI_ORK_ROOT:-.}/lib/pricing_strategy.sh; "
                 f"pricing_lookup openai {model} {kind}"],
                capture_output=True, text=True, timeout=5,
            )
            v = (rc.stdout or "").strip()
            if v and v != "0":
                return float(v)
        except Exception:
            pass
        # 3. Last resort: the hardcoded default that shipped pre-pricing.yaml.
        return float(hardcoded_default)

    p_in = _rate("gpt-5", "input",      "MO_CODEX_USD_PER_MTOK_IN",     "1.25")
    p_cached = _rate("gpt-5", "cache_read", "MO_CODEX_USD_PER_MTOK_CACHED", "0.125")
    p_out = _rate("gpt-5", "output",     "MO_CODEX_USD_PER_MTOK_OUT",    "10.0")
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
  # Pass the stream file path, not its content (ARG_MAX / E2BIG — see above).
  # In this branch RAW_OUT is still the verbatim stream-file cat.
  _CODEX_MSG=$(MO_RAW_FILE="$_CODEX_STREAM_FILE" python3 - <<'PY' || true
import json, os
def _read_raw():
    p = os.environ.get("MO_RAW_FILE", "")
    if not p:
        return ""
    try:
        with open(p, "r", errors="replace") as _f:
            return _f.read()
    except OSError:
        return ""
msgs = []
for line in _read_raw().splitlines():
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
# RAW_OUT is transformed by now (last-message body / agent text), so it can't
# reuse $_CODEX_STREAM_FILE — stage it to a fresh tempfile and pass the path
# (ARG_MAX / E2BIG: never put a large value in env or argv for exec).
_CODEX_CLEAN_IN="$(mktemp -t mini-ork-codex-clean.XXXXXX)"
printf '%s' "$RAW_OUT" > "$_CODEX_CLEAN_IN"
CLEAN=$(MO_RAW_FILE="$_CODEX_CLEAN_IN" python3 - <<'PY'
import re, sys
import os
def _read_raw():
    p = os.environ.get("MO_RAW_FILE", "")
    if not p:
        return ""
    try:
        with open(p, "r", errors="replace") as _f:
            return _f.read()
    except OSError:
        return ""
txt = _read_raw()
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
rm -f "$_CODEX_CLEAN_IN"
[ -z "$CLEAN" ] && CLEAN="$RAW_OUT"

if [ "$FORMAT" = "json" ]; then
  # Emit a minimal claude-shaped JSON envelope so downstream jq parser
  # (lib/llm-dispatch.sh D-04 post-process at lines ~245-255) finds
  # `.result` + `.total_cost_usd`. Codex doesn't expose per-call cost
  # to us via this CLI surface, so total_cost_usd is 0; caller's
  # _d022_charge_node_cost falls back to $0.01 placeholder which is
  # the documented behavior for executable-wrapper lanes.
  # Feed CLEAN via stdin, not argv — a large assistant body as a command-line
  # argument would hit ARG_MAX / E2BIG just like the env-var sites above.
  printf '%s' "$CLEAN" | python3 -c "
import json, sys
print(json.dumps({
    'result': sys.stdin.read(),
    'total_cost_usd': 0.0,
    'model': 'codex',
}))
"
else
  printf '%s\n' "$CLEAN"
fi
