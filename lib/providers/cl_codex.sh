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
# `--output-last-message` gives mini-ork the assistant body instead of the
# terminal transcript (prompt + status + hooks), which would confuse downstream
# JSON extraction. Default to workspace-write because implementer nodes must be
# able to edit the scenario project; operators can override with CODEX_SANDBOX.
_CODEX_LAST_MESSAGE="$(mktemp -t mini-ork-codex-last.XXXXXX)"
_CODEX_SANDBOX="${CODEX_SANDBOX:-workspace-write}"
RAW_OUT=$(codex exec \
  --skip-git-repo-check \
  --sandbox "$_CODEX_SANDBOX" \
  --output-last-message "$_CODEX_LAST_MESSAGE" \
  ${_CODEX_BYO_FLAGS[@]+"${_CODEX_BYO_FLAGS[@]}"} \
  "$PROMPT" 2>&1) || {
  echo "[cl_codex] codex exec failed with rc=$? — see stderr for cause" >&2
  echo "$RAW_OUT" >&2
  rm -f "$_CODEX_LAST_MESSAGE"
  exit 4
}
if [ -s "$_CODEX_LAST_MESSAGE" ]; then
  RAW_OUT="$(cat "$_CODEX_LAST_MESSAGE")"
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
