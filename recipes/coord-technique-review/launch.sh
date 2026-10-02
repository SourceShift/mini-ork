#!/usr/bin/env bash
# Launch the cross-agent coordination technique review on glm-5.3 + MiniMax-M3
# + deepseek-v4-flash ONLY.
#
# Seeds a run-scoped config (runs/<id>/config/{agents,providers}.yaml) that
# maps every lane to those three providers, so no node — pinned or routed,
# including panel/rubric side-channels — can reach an Anthropic, codex, or kimi
# lane. deepseek is pinned to the flash model for this run. The shared
# .mini-ork/config is never touched.
set -euo pipefail
cd "$(dirname "$0")/../.."

RUN_ID="${1:-coord-review-$(date +%Y%m%d-%H%M%S)}"
# Recover mode: MO_COORD_RECOVER_FROM=<node> reruns that node + its dependents
# in an existing run under the same lane policy.
RECOVER_FROM="${MO_COORD_RECOVER_FROM:-}"
HOME_DIR="${MINI_ORK_HOME:-$PWD/.mini-ork}"
RUN_DIR="$HOME_DIR/runs/$RUN_ID"
mkdir -p "$RUN_DIR/config"
if [ -z "$RECOVER_FROM" ]; then

python3 - "$HOME_DIR/config" "$RUN_DIR/config" <<'PY'
import sys, yaml
src, dst = sys.argv[1], sys.argv[2]
allowed = ["glm", "minimax", "deepseek"]
agents = yaml.safe_load(open(f"{src}/agents.yaml"))
for role, value in list(agents["lanes"].items()):
    lanes = [l.strip() for l in str(value).split(",") if l.strip() in allowed]
    agents["lanes"][role] = ",".join(lanes) if lanes else "glm,minimax,deepseek"
agents["lanes"]["glm_lens"] = "glm,minimax,deepseek"
agents["lanes"]["minimax_lens"] = "minimax,glm,deepseek"
agents["lanes"]["deepseek_lens"] = "deepseek,minimax,glm"
yaml.safe_dump(agents, open(f"{dst}/agents.yaml", "w"), sort_keys=False)
providers = yaml.safe_load(open(f"{src}/providers.yaml"))
table = providers.get("providers", providers)
for name in list(table):
    if name not in allowed:
        del table[name]
table["deepseek"]["model"] = "deepseek-v4-flash"
yaml.safe_dump(providers, open(f"{dst}/providers.yaml", "w"), sort_keys=False)
print("lanes:", sorted(set(",".join(agents["lanes"].values()).split(","))),
      "providers:", {k: v.get("model") for k, v in table.items()})
PY
fi

# LibWit token: env wins; else lift the Bearer from a local arxiv-libwit MCP config.
if [ -z "${ARXIV_API_TOKEN:-}" ]; then
  ARXIV_API_TOKEN="$(python3 - "${LIBWIT_MCP_JSON:-/Volumes/docker-ssd/ps/researcher/.mcp.json}" <<'PY'
import json, sys
auth = json.load(open(sys.argv[1]))["mcpServers"]["arxiv-libwit"]["headers"]["Authorization"]
print(auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth)
PY
)"
fi
export ARXIV_API_TOKEN

export MINI_ORK_RUN_ID="$RUN_ID"
export MINI_ORK_RECIPE=coord-technique-review   # recover resolves checkpoints by recipe
export MINI_ORK_PROVIDERS="$RUN_DIR/config/providers.yaml"
export MINI_ORK_COLLECTION_PLAN="${MINI_ORK_COLLECTION_PLAN:-$PWD/recipes/coord-technique-review/collection-plan.json}"
# LibWit hybrid batch search is slow; 32-query batches timed out at the 45s default.
export MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC="${MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC:-900}"
export MINI_ORK_LIBWIT_BATCH_LIMIT="${MINI_ORK_LIBWIT_BATCH_LIMIT:-8}"
export MINI_ORK_RESEARCH_MIN_SOURCES="${MINI_ORK_RESEARCH_MIN_SOURCES:-950}"
export MO_ROUTING_POLICY=static_hybrid     # every researcher node is recipe-pinned
export MO_CHEAP_LANE=minimax_lens
export MO_FRONTIER_LANE=glm_lens           # UCCI escalation target stays in-policy
export MO_FALLBACK_CODING=glm,minimax,deepseek
export MO_FALLBACK_REVIEW=glm,minimax,deepseek
export MINI_ORK_DRY_RUN=0
export MO_DAILY_BUDGET_USD="${MO_DAILY_BUDGET_USD:-150}"
export MO_NODE_TIMEOUT_S="${MO_NODE_TIMEOUT_S:-5400}"   # 100-paper shards can outlive the 1500s default
# The pre-dispatch watchdog fails every queued LLM node when ANY in-flight node's
# heartbeat is older than this (default 300s). Keep it above MO_NODE_TIMEOUT_S.
export MO_HEARTBEAT_TIMEOUT_S="${MO_HEARTBEAT_TIMEOUT_S:-6000}"
export MO_NODE_MAX_TURNS="${MO_NODE_MAX_TURNS:-150}"
export MO_ALLOW_FRAMEWORK_CWD=1            # target is the mini-ork repo itself

echo "run: $RUN_ID  dir: $RUN_DIR"
if [ -n "$RECOVER_FROM" ]; then
  export MINI_ORK_RUN_DIR="$RUN_DIR"
  export MINI_ORK_WORKFLOW="$PWD/recipes/coord-technique-review/workflow.yaml"
  exec bin/mini-ork execute "$RUN_DIR/plan.json" --from-node "$RECOVER_FROM" ${MO_COORD_RECOVER_ARGS:-}
fi
exec bin/mini-ork run coord-technique-review recipes/coord-technique-review/kickoff.md
