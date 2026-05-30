# cl_minimax.sh — route claude --print invocations through MiniMax M2.7.
#
# Source this file in a subshell before invoking `claude` to pin all
# model slots to MiniMax-M2.7 via MiniMax's Anthropic-compatible gateway.
#
# Requires: MINIMAX_API_KEY env var set to your MiniMax API key.
# Example (secrets.local.sh):
#   export MINIMAX_API_KEY=sk-cp-...
#
# See lib/providers/README.md for the secrets.local.sh pattern.

export ANTHROPIC_AUTH_TOKEN="${MINIMAX_API_KEY:?MINIMAX_API_KEY is required — set it in \${MINI_ORK_HOME}/config/secrets.local.sh}"
export ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic
export ANTHROPIC_MODEL=MiniMax-M2.7
export ANTHROPIC_SMALL_FAST_MODEL=MiniMax-M2.7
export ANTHROPIC_DEFAULT_SONNET_MODEL=MiniMax-M2.7
export ANTHROPIC_DEFAULT_OPUS_MODEL=MiniMax-M2.7
export ANTHROPIC_DEFAULT_HAIKU_MODEL=MiniMax-M2.7
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
