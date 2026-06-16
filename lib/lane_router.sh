#!/usr/bin/env bash
# lane_router.sh — GRPO-style relative-advantage lane routing.
#
# Track B item 4 (arXiv:2601.22607 verifiable-reward GRPO + 2603.02701
# Graph-GRPO for multi-agent topologies). When a recipe dispatches a
# lens panel (codex + kimi + glm + minimax in parallel), each lens
# produces a comparable trajectory. We score each by downstream
# verifier outcome and compute:
#
#   relative_advantage[i] = score[i] - mean(scores in group)
#
# Positive advantage = "this lens outperformed peers on this task class".
# Negative = "this lens underperformed". Aggregated into
# agent_performance_memory.relative_advantage and used by the lane
# router to bias future picks toward consistently-winning lanes.
#
# Public API:
#   lane_router_recompute_advantages [--since EPOCH]
#       Walk recent execution_traces grouped by (run_id, task_class,
#       node_type). For each group, compute per-lane scores + group mean,
#       then UPSERT agent_performance_memory rows with relative_advantage.
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

# Group traces by (run_id, task_class) and compute per-lane score within
# group. We derive "lane" from agent_version_id (mini-ork stores it as the
# model_lane string, e.g. "codex_lens"). Score per trace = process_reward
# when present, else a status-derived fallback (success=0.7, failure=0.0,
# other=0.4).
rows = con.execute("""
    SELECT run_id, task_class, agent_version_id,
           verifier_output, status, process_reward
      FROM execution_traces
     WHERE created_at >= ?
       AND task_class IS NOT NULL AND task_class <> ''
       AND agent_version_id IS NOT NULL AND agent_version_id <> ''
       AND run_id IS NOT NULL
""", (since_iso,)).fetchall()

def _score(row):
    pr = row["process_reward"]
    if pr is not None:
        return float(pr)
    st = (row["status"] or "").lower()
    return {"success": 0.70, "failure": 0.0, "vacuous": 0.20,
            "running": 0.40, "blocked": 0.30}.get(st, 0.40)

def _node_type(row):
    # node_type is stored in verifier_output JSON as {"node_type": "..."}
    try:
        vo = json.loads(row["verifier_output"] or "{}")
        return vo.get("node_type") or "unknown"
    except Exception:
        return "unknown"

# Build groups keyed by (run_id, task_class).
groups = defaultdict(list)
for r in rows:
    groups[(r["run_id"], r["task_class"])].append({
        "lane":      r["agent_version_id"],
        "score":     _score(r),
        "task_class": r["task_class"],
        "node_type": _node_type(r),
    })

# Accumulate per (lane, task_class) advantage samples.
acc = defaultdict(lambda: {"adv_sum": 0.0, "n": 0, "wins": 0,
                          "node_types": defaultdict(int)})
for (_run, _tc), members in groups.items():
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
        acc[key]["node_types"][m["node_type"]] += 1

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
