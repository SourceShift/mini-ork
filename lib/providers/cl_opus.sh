# cl_opus.sh — pin claude --print invocations to native Opus 4.7.
#
# Source'd by dispatchers that need Opus as the arbiter / strongest-reasoning
# role. Uses the user's existing Anthropic auth — only pins the model.
#
# Cost note: Opus 4.7 is ~5-10x the per-token cost of Sonnet 4.6, so
# dispatchers should reserve Opus for arbitration / conflict-resolution
# roles where the reasoning quality is load-bearing, not for bulk
# discrimination tasks (those go to Sonnet via cl_sonnet.sh).
export ANTHROPIC_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-opus-4-7
export CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-4-7
