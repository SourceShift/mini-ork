#!/usr/bin/env bash
# secrets.example.sh — template for mini-ork API keys.
#
# Preferred setup: `mini-ork providers configure minimax` prompts securely for
# one lens, while `mini-ork providers configure --workflow <workflow.yaml>`
# discovers all credentials required by a recipe. The command creates this
# file atomically with 0600 permissions and never prints a value.
#
# For automation, pipe NAME=value records to `mini-ork providers configure
# --from-stdin <lane>`; never put a key in a command-line argument. This file
# remains a useful reference and supports existing shell-based workflows.
#
# Path: $MINI_ORK_HOME/config/secrets.local.sh (default:
# .mini-ork/config/secrets.local.sh). It is gitignored
# (.gitignore: **/secrets.local.*) — NEVER commit real keys. Override the
# location with MINI_ORK_SECRETS=/abs/path.
#
# Native Python dispatch parses only simple export NAME=value records from this
# file; it never sources or executes it. Keep it owner-only (0600). Legacy
# Bash dispatch still supports this export format.

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
