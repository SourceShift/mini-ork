# cl_sonnet.sh — pin claude --print invocations to native Sonnet 4.6.
#
# Source'd by dispatchers that need Sonnet specifically. Uses the user's
# existing Anthropic auth (~/.claude/... OAuth or process-env ANTHROPIC_API_KEY);
# only pins the model so the subshell can't fall back to whatever the
# parent process happened to default to.
#
# Mirrors the shape of cl_glm.sh / cl_kimi.sh — pure env-exports, no
# shebang, intended to be source'd in a subshell.
export ANTHROPIC_MODEL=claude-sonnet-4-6
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-sonnet-4-6
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-sonnet-4-6
export CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-4-6
