#!/usr/bin/env bash
# secrets.example.sh — template for mini-ork API keys.
#
# Copy to $MINI_ORK_HOME/config/secrets.local.sh (default:
# .mini-ork/config/secrets.local.sh) and fill in the keys you use.
# That path is gitignored (.gitignore: **/secrets.local.*) — NEVER commit
# real keys. Override the location with MINI_ORK_SECRETS=/abs/path.
#
# lib/llm-dispatch.sh sources this file inside each dispatch subshell,
# so keys exported here are visible to cl_*.sh wrappers and to
# providers.yaml entries via their api_key_env field — but never leak
# into the parent orchestrator process.

# --- BYO keys for config/providers.yaml entries -------------------------

# anthropic_api (kind: anthropic-native) — raw Anthropic API key.
# Leave unset to use your ambient `claude` CLI login instead.
#export ANTHROPIC_API_KEY="sk-ant-..."

# openai_api (kind: openai-compat) — used by codex via env_key.
#export OPENAI_API_KEY="sk-..."

#export OPENROUTER_API_KEY="sk-or-..."

# --- Keys for the committed gateway wrappers (optional lanes) ------------

#export GLM_API_KEY="..."        # cl_glm.sh    (z.ai)
#export KIMI_API_KEY="..."       # cl_kimi.sh   (api.kimi.com)
#export MINIMAX_API_KEY="..."    # cl_minimax.sh (api.minimax.io)
#export DEEPSEEK_API_KEY="..."   # cl_deepseek.sh (api.deepseek.com)
