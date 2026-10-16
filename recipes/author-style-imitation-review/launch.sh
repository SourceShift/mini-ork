#!/usr/bin/env bash
# Launch the author-style-imitation review on glm + minimax ONLY.
#
# Seeds a run-scoped config (runs/<id>/config/{agents,providers}.yaml) mapping
# every lane to glm/minimax, so no node — pinned or routed, including panel or
# rubric side-channels — can reach an Anthropic, codex, kimi, or deepseek lane.
# The shared .mini-ork/config is never touched.
set -euo pipefail
cd "$(dirname "$0")/../.."

RECIPE_DIR="$PWD/recipes/author-style-imitation-review"
RUN_ID="${1:-style-review-$(date +%Y%m%d-%H%M%S)}"
# Recover mode: MO_STYLE_RECOVER_FROM=<node> reruns that node and its
# dependents in an existing run (every other node's artifacts stay as they are)
# under the same lane policy. MO_STYLE_RECOVER_ARGS=--dry-run previews it.
RECOVER_FROM="${MO_STYLE_RECOVER_FROM:-}"
HOME_DIR="${MINI_ORK_HOME:-$PWD/.mini-ork}"
RUN_DIR="$HOME_DIR/runs/$RUN_ID"
mkdir -p "$RUN_DIR/config"

if [ -z "$RECOVER_FROM" ]; then
python3 - "$HOME_DIR/config" "$RUN_DIR/config" <<'PY'
import sys, yaml
src, dst = sys.argv[1], sys.argv[2]
agents = yaml.safe_load(open(f"{src}/agents.yaml"))
allowed = {"glm", "minimax"}
for role, value in list(agents["lanes"].items()):
    lanes = [l.strip() for l in str(value).split(",") if l.strip() in allowed]
    agents["lanes"][role] = ",".join(lanes) if lanes else "glm,minimax"
# The recipe's pinned lenses: each carries half the fan-out, and each falls back
# to the other so a single provider outage degrades rather than stops the run.
agents["lanes"]["glm_lens"] = "glm,minimax"
agents["lanes"]["minimax_lens"] = "minimax,glm"
yaml.safe_dump(agents, open(f"{dst}/agents.yaml", "w"), sort_keys=False)
providers = yaml.safe_load(open(f"{src}/providers.yaml"))
table = providers.get("providers", providers)
for name in list(table):
    if name not in allowed:
        del table[name]
yaml.safe_dump(providers, open(f"{dst}/providers.yaml", "w"), sort_keys=False)
print("lanes:", sorted(set(",".join(agents["lanes"].values()).split(","))), "providers:", sorted(table))
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
export MINI_ORK_RECIPE=author-style-imitation-review   # recover resolves checkpoints by recipe
export MINI_ORK_PROVIDERS="$RUN_DIR/config/providers.yaml"

# --- corpus shape. Keep these in sync with workflow.yaml node counts. ---
export MO_STYLE_SEARCH_BATCHES="${MO_STYLE_SEARCH_BATCHES:-5}"    # searcher_01..05
export MO_STYLE_SHARD_COUNT="${MO_STYLE_SHARD_COUNT:-10}"         # extract_shard_01..10
export MO_STYLE_GROUP_COUNT="${MO_STYLE_GROUP_COUNT:-8}"          # extract_group_01..08
export MINI_ORK_COLLECTION_PLAN="${MINI_ORK_COLLECTION_PLAN:-$RECIPE_DIR/collection-plan.json}"
export MO_STYLE_STATE_FILE="${MO_STYLE_STATE_FILE:-$RECIPE_DIR/context/style-imitation-state.md}"
export MINI_ORK_RESEARCH_MIN_SOURCES="${MINI_ORK_RESEARCH_MIN_SOURCES:-1000}"
export MO_STYLE_MIN_COVERAGE="${MO_STYLE_MIN_COVERAGE:-0.95}"

# --- LibWit transport. Hybrid batch search is slow: a 32-query batch timed out
# at the 45s default, and five searchers fire at once here, so the timeout is
# generous and the per-request fan-out is capped by the plan's max_workers. ---
export MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC="${MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC:-900}"

# --- lanes. Every researcher node is recipe-pinned, so no routing surprises. ---
export MO_ROUTING_POLICY=static_hybrid
export MO_CHEAP_LANE=minimax_lens
export MO_FRONTIER_LANE=glm_lens
export MO_FALLBACK_CODING=glm,minimax
export MO_FALLBACK_REVIEW=glm,minimax

# --- budget and timeouts. A 100-paper shard is a long single turn; the
# pre-dispatch watchdog fails every queued LLM node when ANY in-flight node's
# heartbeat is older than its limit, so keep that above MO_NODE_TIMEOUT_S. ---
export MINI_ORK_DRY_RUN=0
export MO_DAILY_BUDGET_USD="${MO_DAILY_BUDGET_USD:-150}"
export MO_NODE_TIMEOUT_S="${MO_NODE_TIMEOUT_S:-5400}"
export MO_HEARTBEAT_TIMEOUT_S="${MO_HEARTBEAT_TIMEOUT_S:-6000}"
export MO_NODE_MAX_TURNS="${MO_NODE_MAX_TURNS:-150}"
export MO_ALLOW_FRAMEWORK_CWD=1   # the target is the mini-ork repo itself

echo "run: $RUN_ID  dir: $RUN_DIR"
if [ -n "$RECOVER_FROM" ]; then
  # `mini-ork recover` only prints the closure from the CLI (its executor
  # hand-off is in-process); execute --from-node dispatches the same closure.
  export MINI_ORK_RUN_DIR="$RUN_DIR"
  export MINI_ORK_WORKFLOW="$RECIPE_DIR/workflow.yaml"
  exec bin/mini-ork execute "$RUN_DIR/plan.json" --from-node "$RECOVER_FROM" ${MO_STYLE_RECOVER_ARGS:-}
fi
exec bin/mini-ork run author-style-imitation-review "$RECIPE_DIR/kickoff.md"
