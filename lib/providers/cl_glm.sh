# cl_glm.sh — route claude --print invocations through GLM (Z.AI).
#
# Source this file in a subshell before invoking `claude` to pin all
# model slots to GLM-5.1 via the Z.AI Anthropic-compatible gateway.
#
# Requires: GLM_API_KEY env var set to your Z.AI API key.
# Format: <hex32>.<random>  (e.g. 406c14eba1d64310be302d6acef78ee9.cDDPLdWiNT9GJSsU)
# Example (secrets.local.sh):
#   export GLM_API_KEY=406c14eba1d...
#
# See lib/providers/README.md for the secrets.local.sh pattern.

export ANTHROPIC_AUTH_TOKEN="${GLM_API_KEY:?GLM_API_KEY is required - set it in MINI_ORK_HOME/config/secrets.local.sh}"
export ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic
export ANTHROPIC_MODEL=GLM-5.1
export ANTHROPIC_SMALL_FAST_MODEL=GLM-5.1
export ANTHROPIC_DEFAULT_SONNET_MODEL=GLM-5.1
export ANTHROPIC_DEFAULT_OPUS_MODEL=GLM-5.1
export ANTHROPIC_DEFAULT_HAIKU_MODEL=GLM-5.1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
