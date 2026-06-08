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

export ANTHROPIC_AUTH_TOKEN="${KIMI_API_KEY:?KIMI_API_KEY is required - set it in MINI_ORK_HOME/config/secrets.local.sh}"
export ANTHROPIC_BASE_URL=https://api.kimi.com/coding/
# Live gateway validation on 2026-06-08: the Kimi Anthropic-compatible
# endpoint used by this environment returns HTTP 400 for kimi-for-coding via
# Claude Code, while the local working wrapper succeeds with kimi-k2.6.
export ANTHROPIC_MODEL=kimi-k2.6
export ANTHROPIC_DEFAULT_OPUS_MODEL=kimi-k2.6
export ANTHROPIC_DEFAULT_SONNET_MODEL=kimi-k2.6
export ANTHROPIC_DEFAULT_HAIKU_MODEL=kimi-k2.6
export CLAUDE_CODE_SUBAGENT_MODEL=kimi-k2.6
export ENABLE_TOOL_SEARCH=false
