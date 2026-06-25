#!/usr/bin/env bash
# lane_router.sh — GRPO-style relative-advantage lane routing.
#
# Track B item 4 (arXiv:2601.22607 verifiable-reward GRPO + 2603.02701
# Graph-GRPO for multi-agent topologies). When a recipe dispatches a
# lens panel (codex + kimi + glm + minimax in parallel), each lens
# produces a comparable trajectory. We score each by the normalized
# cross-objective reward_g (set by trace_store.sh from
# reward_value/anchor/direction) and compute:
#
#   relative_advantage[i] = score[i] - mean(scores in group)
#
# The group key is (objective_domain, task_class, node_type) so each
# objective domain learns an isolated policy signal. score is sourced
# only from reward_g; rows with reward_g IS NULL are skipped — falling
# back to process_reward or status would reintroduce raw cross-objective
# reward contamination the normalized reward_g column exists to remove.
#
# Positive advantage = "this lens outperformed peers on this
# objective_domain + task_class + node_type". Negative = "this lens
# underperformed". Aggregated into agent_performance_memory.
# relative_advantage and used by the lane router to bias future picks
# toward consistently-winning lanes.
#
# Public API:
#   lane_router_recompute_advantages [--since EPOCH]
#       Walk recent execution_traces grouped by (objective_domain,
#       task_class, node_type). For each group, compute per-lane scores
#       from normalized reward_g plus the group mean, then UPSERT
#       agent_performance_memory rows with relative_advantage. Rows
#       with reward_g IS NULL are skipped.
#
#   lane_router_preferred_lane <task_class> <node_type>
#       Print the lane (model lane name) with highest relative_advantage
#       for the given pair, with sample_size >= 3 to avoid noise.
#       Falls back to the lane configured in agents.yaml when no data.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

lane_router_recompute_advantages() {
  local since=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --since) since="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  python3 - "$STATE_DB" "$since" <<'PY'
import json, sqlite3, sys, datetime
from collections import defaultdict

db, since_str = sys.argv[1:3]
since_iso = datetime.datetime.utcfromtimestamp(int(since_str)).strftime(
    '%Y-%m-%dT%H:%M:%S.000Z'
)

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row

# Group traces by (objective_domain, task_class, node_type) so each
# objective domain learns an isolated policy signal. "lane" derives from
# agent_version_id (mini-ork stores it as the model_lane string, e.g.
# "codex_lens"). score is sourced only from the normalized reward_g column
# (set by trace_store.sh from reward_value/anchor/direction); rows with
# reward_g IS NULL are skipped so unnormalized raw signals never enter
# the relative-advantage calculation.
rows = con.execute("""
    SELECT objective_domain, task_class, agent_version_id,
           verifier_output, reward_g
      FROM execution_traces
     WHERE created_at >= ?
       AND task_class IS NOT NULL AND task_class <> ''
       AND agent_version_id IS NOT NULL AND agent_version_id <> ''
       AND objective_domain IS NOT NULL AND objective_domain <> ''
       AND reward_g IS NOT NULL
""", (since_iso,)).fetchall()

def _score(row):
    # score is the normalized cross-objective reward_g only. No fallback to
    # process_reward or status — that would reintroduce raw cross-objective
    # reward contamination the reward_g column exists to remove.
    return float(row["reward_g"])

def _node_type(row):
    # node_type is stored in verifier_output JSON as {"node_type": "..."}
    try:
        vo = json.loads(row["verifier_output"] or "{}")
        return vo.get("node_type") or "unknown"
    except Exception:
        return "unknown"

# Build groups keyed by (objective_domain, task_class, node_type).
groups = defaultdict(list)
for r in rows:
    groups[(r["objective_domain"], r["task_class"], _node_type(r))].append({
        "lane":      r["agent_version_id"],
        "score":     _score(r),
        "task_class": r["task_class"],
    })

# Accumulate per (lane, task_class) advantage samples. The slice group
# key (objective_domain, task_class, node_type) provides domain-isolated
# computation here; the storage row key collapses to (lane, task_class)
# because agent_performance_memory.PRIMARY KEY is (agent_version_id,
# task_class) — per-domain UPSERTs collide on storage. Domain isolation
# therefore lives at the slice-mean level, not the storage row.
acc = defaultdict(lambda: {"adv_sum": 0.0, "n": 0, "wins": 0,
                          "node_types": defaultdict(int),
                          "objective_domains": defaultdict(int)})
for (_od, _tc, _nt), members in groups.items():
    if len(members) < 2:
        continue  # need at least 2 lenses for group-relative comparison
    mean = sum(m["score"] for m in members) / len(members)
    for m in members:
        adv = m["score"] - mean
        key = (m["lane"], m["task_class"])
        acc[key]["adv_sum"] += adv
        acc[key]["n"] += 1
        if adv > 0:
            acc[key]["wins"] += 1
        # node_type + objective_domain are folded for traceability even
        # though storage PK doesn't separate them; once a future migration
        # adds objective_domain to agent_performance_memory this folding
        # can be promoted to a real column.
        acc[key]["node_types"][_nt] += 1
        acc[key]["objective_domains"][_od] += 1

# Upsert agent_performance_memory.
# PRIMARY KEY is (agent_version_id, task_class) so we collapse node_types
# into the most common one per (lane, task_class).
upserted = 0
for (lane, tc), stats in acc.items():
    avg_adv = stats["adv_sum"] / max(stats["n"], 1)
    top_node = max(stats["node_types"].items(), key=lambda kv: kv[1])[0] \
               if stats["node_types"] else None
    success_rate = stats["wins"] / max(stats["n"], 1)
    con.execute("""
        INSERT INTO agent_performance_memory
            (agent_version_id, role, model, task_class,
             runs_count, success_count,
             relative_advantage,
             last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(agent_version_id, task_class) DO UPDATE SET
            role               = excluded.role,
            model              = excluded.model,
            runs_count         = excluded.runs_count,
            success_count      = excluded.success_count,
            relative_advantage = excluded.relative_advantage,
            last_updated       = excluded.last_updated
    """, (lane, top_node or lane, lane, tc,
          stats["n"], stats["wins"], round(avg_adv, 4)))
    upserted += 1
con.commit()
con.close()
print(upserted)
PY
}

lane_router_preferred_lane() {
  local task_class="${1:?task_class required}"
  local node_type="${2:-}"
  local where="task_class='$task_class' AND runs_count >= 3"
  [ -n "$node_type" ] && where="$where AND (role='$node_type' OR model='$node_type')"
  sqlite3 -separator '|' "$STATE_DB" \
    "SELECT agent_version_id, printf('%.3f', relative_advantage), runs_count
       FROM agent_performance_memory
      WHERE $where
      ORDER BY relative_advantage DESC, runs_count DESC
      LIMIT 1;"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "lane_router.sh — source me and call lane_router_recompute_advantages / lane_router_preferred_lane"
fi
