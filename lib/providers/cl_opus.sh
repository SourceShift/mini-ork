# cl_opus.sh — route claude --print invocations through Anthropic Opus 4.7
# using the operator's ambient Claude Code login.
#
# 2026-06-09 policy change: Anthropic-native wrappers (cl_opus, cl_sonnet)
# must NOT export ANTHROPIC_* env vars. The operator is already logged in
# via Claude Code; claude --print inherits that session's auth + selected
# model when NO Anthropic-related env vars are set. Setting them OVERRIDES
# the Claude Code session and tends to break in subtle ways (e.g. session
# is Opus but ANTHROPIC_MODEL pins claude-opus-4-7 → fine; session is
# Sonnet but ANTHROPIC_MODEL=claude-opus-4-7 → silent auth mismatch).
#
# This wrapper's only legitimate job: clear gateway pollution. When a
# previous wrapper in the same shell (cl_glm / cl_kimi / cl_minimax /
# cl_deepseek) exported ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL pointing
# at a non-Anthropic gateway, sourcing cl_opus.sh WITHOUT undoing those
# exports means claude --print sends an Anthropic-model-name to the
# gateway URL and gets back 400/401. Iters 1-5 of session-9's v4 launch
# spun for exactly this reason after secrets.local.sh's `set -a; . kimi.env`
# globally exported ANTHROPIC_AUTH_TOKEN.
#
# Result: this wrapper UNSETS gateway-leakage vars, then exits without
# setting anything new. claude --print falls back to ambient Claude Code
# auth and model selection.

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
