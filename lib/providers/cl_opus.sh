# cl_opus.sh — pin claude --print invocations to native Opus 4.7.
#
# Source'd by dispatchers that need Opus as the arbiter / strongest-reasoning
# role. Uses the user's existing Anthropic auth — only pins the model.
#
# Cost note: Opus 4.7 is ~5-10× the per-token cost of Sonnet 4.6 and ~25-90×
# Haiku. Reserve Opus for arbitration / conflict-resolution roles where the
# reasoning quality is load-bearing.
#
# v0.2-pt18 (W3 from DF12 audit synthesis, refactor-audit/synthesis-latest.md
# Section 2.3): split the model-slot env vars so SUB-AGENTS that an Opus
# session spawns (TodoWrite, Agent, file reads, etc) run on Sonnet/Haiku
# instead of the main Opus tier. Previous behavior pinned EVERY slot to
# Opus, inflating sub-agent cost ~25-90×. At 100K runs/day with frequent
# reviewer fan-out, audit estimated ~$8K/day saved by this split.
#
# Override: set MO_OPUS_PIN_ALL=1 in the parent shell BEFORE sourcing this
# script to restore the legacy all-Opus behavior (rare — only for brain
# sessions where sub-agent reasoning quality is genuinely load-bearing).
export ANTHROPIC_MODEL=claude-opus-4-7
export ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7

if [ "${MO_OPUS_PIN_ALL:-0}" = "1" ]; then
  # Legacy override: every slot on Opus (5-10× cost on sub-agents)
  export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-opus-4-7
  export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-opus-4-7
  export CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-4-7
else
  # v0.2-pt18 default: main Opus, sub-agents on cheaper tiers
  export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
  export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5-20251001
  export CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-4-6
fi
