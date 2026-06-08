# cl_deepseek.sh — DeepSeek V4 via Anthropic-compatible endpoint.
# Source this file before invoking `claude --bare` to route the SDK to DeepSeek.
#
# Two model families:
#   deepseek-v4-pro[1m]   — high-quality reasoning, 1M ctx (Opus/Sonnet slot)
#   deepseek-v4-flash     — fast/cheap (Haiku slot, sub-agents)
#
# Requires: DEEPSEEK_API_KEY env var set to your DeepSeek API key.
# Example (secrets.local.sh):
#   export DEEPSEEK_API_KEY=sk-...
#
# See lib/providers/README.md for the secrets.local.sh pattern.

export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_AUTH_TOKEN="${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY is required - set it in MINI_ORK_HOME/config/secrets.local.sh}"
export ANTHROPIC_MODEL='deepseek-v4-pro[1m]'
export ANTHROPIC_DEFAULT_OPUS_MODEL='deepseek-v4-pro[1m]'
export ANTHROPIC_DEFAULT_SONNET_MODEL='deepseek-v4-pro[1m]'
export ANTHROPIC_DEFAULT_HAIKU_MODEL='deepseek-v4-flash'
export CLAUDE_CODE_SUBAGENT_MODEL='deepseek-v4-flash'
export CLAUDE_CODE_EFFORT_LEVEL=high
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
