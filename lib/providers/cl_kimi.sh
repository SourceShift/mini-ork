# cl_kimi.sh — route claude --print invocations through Kimi K2.
#
# Source this file in a subshell before invoking `claude` to pin all
# model slots to Kimi's Anthropic-compatible endpoint.
#
# Requires: KIMI_API_KEY env var set to your Kimi API key.
# Example (secrets.local.sh):
#   export KIMI_API_KEY=sk-kimi-...
#
# See lib/providers/README.md for the secrets.local.sh pattern.

export ANTHROPIC_AUTH_TOKEN="${KIMI_API_KEY:?KIMI_API_KEY is required — set it in \${MINI_ORK_HOME}/config/secrets.local.sh}"
export ANTHROPIC_BASE_URL=https://api.kimi.com/coding/
# Stable model ID per official Kimi Code docs (kimi.com/code/docs/en):
# "always use the model ID `kimi-for-coding` ... the backend automatically
# updates the display name it maps to whenever a newer model is released."
# A version-pinned display name like `kimi-k2.6` is a model DISPLAY NAME,
# not the routing slug — using it can route via a degraded fallback path
# where tool-loop detection misfires (openclaw#71273 documents the same
# infinite-tool-loop symptom).
export ANTHROPIC_MODEL=kimi-for-coding
export ANTHROPIC_DEFAULT_OPUS_MODEL=kimi-for-coding
export ANTHROPIC_DEFAULT_SONNET_MODEL=kimi-for-coding
export ANTHROPIC_DEFAULT_HAIKU_MODEL=kimi-for-coding
export CLAUDE_CODE_SUBAGENT_MODEL=kimi-for-coding
export ENABLE_TOOL_SEARCH=false
