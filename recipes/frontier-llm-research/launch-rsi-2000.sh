#!/usr/bin/env bash
# Launch the 2000-paper RSI scan on the deepseek_flash researcher lane.
# Run-scoped wiring only: pre-seeded runs/<id>/config/{agents,providers}.yaml
# via MINI_ORK_RUN_ID + MINI_ORK_PROVIDERS; the shared .mini-ork/config is
# never touched (a live goal-loop reads it).
set -euo pipefail
cd "$(dirname "$0")/../.."

RUN_ID="${1:?usage: launch-rsi-2000.sh <run-id with pre-seeded config dir>}"
RUN_DIR="$PWD/.mini-ork/runs/$RUN_ID"
[ -f "$RUN_DIR/config/agents.yaml" ] || { echo "no pre-seeded agents.yaml in $RUN_DIR/config" >&2; exit 2; }
[ -f "$RUN_DIR/config/providers.yaml" ] || { echo "no pre-seeded providers.yaml in $RUN_DIR/config" >&2; exit 2; }

# Live deepseek key (the secrets-store DEEPSEEK_API_KEY is balance-dead;
# real env wins over the store's setdefault).
DEEPSEEK_API_KEY="$(grep -m1 '^MINIMAX_AUTH_TOKEN=' /Volumes/docker-ssd/ps/libwit_v1/.env | cut -d= -f2- | tr -d '"')"
export DEEPSEEK_API_KEY
ARXIV_API_TOKEN="${ARXIV_API_TOKEN:-$(ssh REDACTED-INTERNAL-HOST 'grep -m1 "^ARXIV_API_TOKEN=" /srv/REDACTED/.env | cut -d= -f2-')}"
export ARXIV_API_TOKEN

export MINI_ORK_RUN_ID="$RUN_ID"
export MINI_ORK_PROVIDERS="$RUN_DIR/config/providers.yaml"
export MINI_ORK_COLLECTION_PLAN="$PWD/recipes/frontier-llm-research/collection-plan.miniork-rsi-2000.json"
export MINI_ORK_RESEARCH_MIN_SOURCES=200   # graceful floor; corpus itself is 2000
export MO_ROUTING_POLICY=static_hybrid     # frontier/cheap policy; no learned rerouting
export MO_CHEAP_LANE=deepseek_flash        # static_hybrid routes UNPINNED researcher nodes to the cheap
                                           # lane (default kimi_lens) — the agents.yaml researcher line
                                           # is NOT consulted by this policy
export MO_FALLBACK_CODING=glm,minimax      # never walk dead codex/sonnet tails
export MO_FALLBACK_REVIEW=glm,minimax
export MINI_ORK_DRY_RUN=0
export MO_DAILY_BUDGET_USD=100
export MO_NODE_TIMEOUT_S=5400              # 200-paper shards outlive the 1500s default
export MO_NODE_MAX_TURNS=120
export MO_ALLOW_FRAMEWORK_CWD=1   # run targets the mini-ork repo itself; without it the cwd guard masks as "planner rc=2"

exec bin/mini-ork run frontier-llm-research recipes/frontier-llm-research/rsi-kickoff.md
