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

# Invoke codex exec. The `--skip-git-repo-check` flag avoids the prompt
# that codex emits when not in a git repo; we may run from /tmp / .mini-ork/runs/.
# Capture stdout; codex writes its own status to stderr which we let through.
# Note: codex exec output shape:
#   - default: streaming text to stdout (assistant message + tool calls inline)
#   - we extract just the text by piping through a simple grep filter that
#     skips lines starting with `[codex]` (status) and `tokens used:` (footer)
RAW_OUT=$(codex exec --skip-git-repo-check "$PROMPT" 2>&1) || {
  echo "[cl_codex] codex exec failed with rc=$? — see stderr for cause" >&2
  echo "$RAW_OUT" >&2
  exit 4
}

# Strip codex's wrapper lines so downstream parsers see a clean text body.
# Keep everything except status banners and final-token-count footer.
CLEAN=$(echo "$RAW_OUT" | grep -vE '^\[20[0-9]{2}-[0-9]{2}-[0-9]{2}T' \
                       | grep -vE '^tokens used:' \
                       | grep -vE '^User instructions:' \
                       | grep -vE '^OpenAI Codex' \
                       || echo "$RAW_OUT")

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
