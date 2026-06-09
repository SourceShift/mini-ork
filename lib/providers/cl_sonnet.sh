# cl_sonnet.sh — route claude --print invocations through Anthropic Sonnet
# using the operator's ambient Claude Code login.
#
# Same policy as cl_opus.sh (see that file's header comment for the full
# rationale): Anthropic-native wrappers must NOT export ANTHROPIC_* env
# vars. The operator is logged in via Claude Code and claude --print
# inherits that session's auth + tier when no env overrides exist.
#
# This wrapper only clears gateway pollution left by prior wrappers
# (cl_glm / cl_kimi / cl_minimax / cl_deepseek) so claude --print doesn't
# route an Anthropic model name to a non-Anthropic gateway URL.

unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_API_KEY
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_MODEL
unset ANTHROPIC_DEFAULT_OPUS_MODEL
unset ANTHROPIC_DEFAULT_SONNET_MODEL
unset ANTHROPIC_DEFAULT_HAIKU_MODEL
unset ANTHROPIC_SMALL_FAST_MODEL
unset CLAUDE_CODE_SUBAGENT_MODEL
unset CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
