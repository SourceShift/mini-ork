#!/usr/bin/env bash
# Launch the agent-security futures briefing on glm + minimax ONLY.
#
# Seeds a run-scoped config (runs/<id>/config/{agents,providers}.yaml) that
# maps every lane to glm/minimax, so no node — pinned or routed, including
# panel/rubric side-channels — can reach an Anthropic, codex, kimi, or
# deepseek lane. The shared .mini-ork/config is never touched.
set -euo pipefail
cd "$(dirname "$0")/../.."

RUN_ID="${1:-agent-sec-futures-$(date +%Y%m%d-%H%M%S)}"
# Recover mode: MO_SEC_RECOVER_FROM=<node> reruns that node + its dependents in
# an existing run (every other node's artifacts stay as they are) under the
# same lane policy. MO_SEC_RECOVER_ARGS=--dry-run previews the closure.
RECOVER_FROM="${MO_SEC_RECOVER_FROM:-}"
HOME_DIR="${MINI_ORK_HOME:-$PWD/.mini-ork}"
RUN_DIR="$HOME_DIR/runs/$RUN_ID"
mkdir -p "$RUN_DIR/config"

# Config base: the family-wide lane/provider templates. Absent on a checkout
# that never ran `mini-ork init`, so say what to do instead of letting YAML
# open() the missing path with a traceback three frames deep.
CONFIG_SRC="${MINI_ORK_CONFIG_SRC:-$HOME_DIR/config}"
for f in agents.yaml providers.yaml; do
  [ -f "$CONFIG_SRC/$f" ] || {
    echo "launch: no $CONFIG_SRC/$f — run 'bin/mini-ork init' first, or set MINI_ORK_HOME / MINI_ORK_CONFIG_SRC" >&2
    exit 2
  }
done

if [ -z "$RECOVER_FROM" ]; then

python3 - "$CONFIG_SRC" "$RUN_DIR/config" <<'PY'
import sys, yaml
src, dst = sys.argv[1], sys.argv[2]
agents = yaml.safe_load(open(f"{src}/agents.yaml"))
allowed = {"glm", "minimax"}
for role, value in list(agents["lanes"].items()):
    lanes = [l.strip() for l in str(value).split(",") if l.strip() in allowed]
    agents["lanes"][role] = ",".join(lanes) if lanes else "glm,minimax"
# Without these two the lens roles both fall back to "glm,minimax", resolve to
# one family, and the two "independent" forecast reviewers are the same model
# twice. Pin each lens to its own family, with the other as fallback.
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

# The run-scoped lane map only restrains dispatch when the engine's overlay
# resolver is pointed at it; MINI_ORK_AGENTS is that pointer. providers.yaml
# already restricts the provider set, this closes the lane half.
if [ -f "$RUN_DIR/config/agents.yaml" ]; then
  export MINI_ORK_AGENTS="$RUN_DIR/config/agents.yaml"
fi

# LibWit token: env wins; else lift the Bearer from a local arxiv-libwit MCP
# config. The path is machine-local by design — absent path, unreadable JSON,
# or empty token are all a clear stop, never a silent unauthenticated run.
if [ -z "${ARXIV_API_TOKEN:-}" ]; then
  MCP_JSON="${LIBWIT_MCP_JSON:-/Volumes/docker-ssd/ps/researcher/.mcp.json}"
  if [ ! -f "$MCP_JSON" ]; then
    echo "launch: ARXIV_API_TOKEN unset and no arxiv-libwit MCP config at $MCP_JSON — export ARXIV_API_TOKEN or set LIBWIT_MCP_JSON" >&2
    exit 2
  fi
  ARXIV_API_TOKEN="$(python3 - "$MCP_JSON" <<'PY'
import json, sys
try:
    auth = json.load(open(sys.argv[1]))["mcpServers"]["arxiv-libwit"]["headers"]["Authorization"]
except (OSError, ValueError, KeyError, TypeError) as exc:
    sys.exit(f"launch: cannot read the LibWit bearer from {sys.argv[1]}: {exc}")
token = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
if not token.strip():
    sys.exit("launch: the LibWit Authorization header holds no token")
print(token.strip())
PY
)" || exit 2
  [ -n "$ARXIV_API_TOKEN" ] || { echo "launch: empty ARXIV_API_TOKEN" >&2; exit 2; }
fi
export ARXIV_API_TOKEN

export MINI_ORK_RUN_ID="$RUN_ID"
export MINI_ORK_RECIPE=agent-security-futures   # recover resolves checkpoints by recipe
export MINI_ORK_PROVIDERS="$RUN_DIR/config/providers.yaml"
export MO_SECURITY_STATE_FILE="${MO_SECURITY_STATE_FILE:-$PWD/recipes/agent-security-futures/context/preparedness-state.md}"
export MINI_ORK_COLLECTION_PLAN="${MINI_ORK_COLLECTION_PLAN:-$PWD/recipes/agent-security-futures/collection-plan.json}"
# LibWit hybrid batch search is slow; 32-query batches timed out at the 45s default.
export MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC="${MINI_ORK_LIBWIT_REQUEST_TIMEOUT_SEC:-900}"
export MINI_ORK_LIBWIT_BATCH_LIMIT="${MINI_ORK_LIBWIT_BATCH_LIMIT:-8}"
export MINI_ORK_RESEARCH_MIN_SOURCES="${MINI_ORK_RESEARCH_MIN_SOURCES:-1900}"
export MO_ROUTING_POLICY=static_hybrid     # every researcher node is recipe-pinned
export MO_CHEAP_LANE=minimax_lens
export MO_FRONTIER_LANE=glm_lens
export MO_FALLBACK_CODING=glm,minimax
export MO_FALLBACK_REVIEW=glm,minimax
export MINI_ORK_DRY_RUN=0
export MO_DAILY_BUDGET_USD="${MO_DAILY_BUDGET_USD:-150}"
export MO_NODE_TIMEOUT_S="${MO_NODE_TIMEOUT_S:-5400}"   # 200-paper shards outlive the 1500s default
# The pre-dispatch watchdog fails every queued LLM node when ANY in-flight node's
# heartbeat is older than this (default 300s); one long 200-paper turn killed
# shards 05-10 on an earlier full run. Keep it above MO_NODE_TIMEOUT_S.
export MO_HEARTBEAT_TIMEOUT_S="${MO_HEARTBEAT_TIMEOUT_S:-6000}"
export MO_NODE_MAX_TURNS="${MO_NODE_MAX_TURNS:-150}"
export MO_ALLOW_FRAMEWORK_CWD=1            # target is the mini-ork repo itself

echo "run: $RUN_ID  dir: $RUN_DIR"
if [ -n "$RECOVER_FROM" ]; then
  # `mini-ork recover` only prints the closure from the CLI (its executor
  # hand-off is in-process); execute --from-node dispatches the same closure.
  export MINI_ORK_RUN_DIR="$RUN_DIR"
  export MINI_ORK_WORKFLOW="$PWD/recipes/agent-security-futures/workflow.yaml"
  exec bin/mini-ork execute "$RUN_DIR/plan.json" --from-node "$RECOVER_FROM" ${MO_SEC_RECOVER_ARGS:-}
fi
exec bin/mini-ork run agent-security-futures recipes/agent-security-futures/kickoff.md
