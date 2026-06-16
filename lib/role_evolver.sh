#!/usr/bin/env bash
# role_evolver.sh — propose role/responsibility mutations from observed
# signals. Phase 1 of the meta-orchestrator design.
#
# Grounded in EvoChamber arXiv:2605.11136 (coordinated co-evolution of
# role specializations) and "Who Am I, and Who Else Is Here?" 2604.00026
# (behavioral differentiation from feedback, no explicit assignment).
#
# Reads three sources:
#   1. agent_performance_memory.relative_advantage (GRPO router signal)
#   2. bug_reports filtered by agent_role
#   3. gradient_records with target starting 'agent.<role>.' or 'workflow.node.'
#
# Emits role_evolver_log proposals. The conductor (Phase 2) reads
# status='open' proposals when deciding the next epic's role assignment.
#
# Public API:
#   role_evolver_propose [--task-class X] [--top N]
#       Generate proposals from current signals. Idempotent: same
#       (target_recipe, target_node_id, proposal_kind) skipped if already
#       open. Returns count of new proposals.
#
#   role_evolver_list [--status STATUS]
#       List recent proposals.
#
#   role_evolver_accept <id>     Flip status to 'accepted'.
#   role_evolver_reject <id>     Flip status to 'rejected' + comment.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

role_evolver_propose() {
  local class_filter=""
  local top=5
  while [ $# -gt 0 ]; do
    case "$1" in
      --task-class) class_filter="$2"; shift 2 ;;
      --top)        top="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  python3 - "$STATE_DB" "$class_filter" "$top" <<'PY'
import json, sqlite3, sys, time
db, class_filter, top_s = sys.argv[1:4]
top = int(top_s)

con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row

proposals = []

# Signal 1: lanes with strongly negative relative_advantage in their
# (lane, task_class) combo. Propose RETIRE — stop routing this lane here.
clauses = "runs_count >= 3 AND relative_advantage <= -0.20"
if class_filter:
    clauses += f" AND task_class='{class_filter}'"
losers = con.execute(f"""
    SELECT agent_version_id AS lane, task_class, role, model,
           relative_advantage, runs_count
      FROM agent_performance_memory
     WHERE {clauses}
     ORDER BY relative_advantage ASC LIMIT ?
""", (top,)).fetchall()
for r in losers:
    proposals.append({
        "target_recipe":   r["task_class"].replace("_", "-"),
        "target_node_id":  r["lane"],
        "proposal_kind":   "retire",
        "rationale":       (f"Lane {r['lane']} has relative_advantage "
                            f"{r['relative_advantage']:.2f} over "
                            f"{r['runs_count']} runs in {r['task_class']} — "
                            f"consistently underperforms its peers."),
        "evidence_json":   json.dumps({"agent_perf_rows": [r["lane"]]}),
        "proposed_change": f"# Remove {r['lane']} from {r['task_class']} lens panel",
    })

# Signal 2: high-frequency bug_reports tagged with an agent_role — propose
# SPLIT — separate the role into two narrower responsibilities.
bug_clusters = con.execute("""
    SELECT agent_role, COUNT(*) AS n, MAX(frequency) AS max_freq,
           GROUP_CONCAT(id) AS bug_ids,
           (SELECT title FROM bug_reports b2
              WHERE b2.agent_role = b.agent_role AND b2.status='open'
              ORDER BY severity='critical' DESC, frequency DESC LIMIT 1
           ) AS top_title
      FROM bug_reports b
     WHERE status='open' AND severity IN ('high','critical')
     GROUP BY agent_role
    HAVING n >= 2
     ORDER BY n DESC LIMIT ?
""", (top,)).fetchall()
for r in bug_clusters:
    if not r["agent_role"]:
        continue
    proposals.append({
        "target_recipe":   "framework-edit",  # default; conductor refines
        "target_node_id":  r["agent_role"],
        "proposal_kind":   "split",
        "rationale":       (f"Role {r['agent_role']} accumulated {r['n']} "
                            f"high/critical bug_reports (top title: "
                            f"{(r['top_title'] or '')[:80]!r}). Splitting "
                            f"into focused sub-roles may reduce surface."),
        "evidence_json":   json.dumps({"bug_ids": (r["bug_ids"] or "").split(",")[:10]}),
        "proposed_change": f"# Split {r['agent_role']} into {r['agent_role']}_pre and {r['agent_role']}_post",
    })

# Signal 3: workflow nodes appearing in cross_class gradients with high
# confidence — propose RENAME (the role is generalizing across classes,
# so name it more abstractly).
cross_targets = con.execute("""
    SELECT target, MAX(confidence) AS top_conf, COUNT(*) AS n
      FROM gradient_records
     WHERE task_class='__cross_class__'
       AND target LIKE 'cross_class:workflow.node.%'
       AND confidence >= 0.85
     GROUP BY target
     ORDER BY top_conf DESC, n DESC LIMIT ?
""", (top,)).fetchall()
for r in cross_targets:
    # Extract the node name from 'cross_class:workflow.node.<name>'
    node_name = r["target"].split(".")[-1] if r["target"] else "?"
    if not node_name or node_name == "?":
        continue
    proposals.append({
        "target_recipe":   "*",
        "target_node_id":  node_name,
        "proposal_kind":   "rename",
        "rationale":       (f"Node '{node_name}' surfaces as a "
                            f"__cross_class__ gradient with confidence "
                            f"{r['top_conf']:.2f}. Lessons generalize — "
                            f"consider naming abstractly."),
        "evidence_json":   json.dumps({"gradient_target": r["target"]}),
        "proposed_change": f"# Rename node {node_name} -> <abstract noun>",
    })

# Insert with idempotence on (target_recipe, target_node_id, proposal_kind).
inserted = 0
now = int(time.time())
for p in proposals:
    exists = con.execute("""
        SELECT 1 FROM role_evolver_log
         WHERE target_recipe=? AND target_node_id=? AND proposal_kind=?
           AND status='open' LIMIT 1
    """, (p["target_recipe"], p["target_node_id"], p["proposal_kind"])).fetchone()
    if exists:
        continue
    con.execute("""
        INSERT INTO role_evolver_log
            (proposed_at, target_recipe, target_node_id, proposal_kind,
             rationale, evidence_json, proposed_change, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
    """, (now, p["target_recipe"], p["target_node_id"], p["proposal_kind"],
          p["rationale"][:600], p["evidence_json"][:2000],
          p["proposed_change"][:600]))
    inserted += 1

con.commit(); con.close()
print(inserted)
PY
}

role_evolver_list() {
  local status="${1:---open}"
  status="${status#--}"
  local clause=""
  [ -n "$status" ] && [ "$status" != "all" ] && clause="WHERE status='$status'"
  sqlite3 -separator ' | ' "$STATE_DB" \
    "SELECT printf('%-4d', id),
            printf('%-10s', status),
            printf('%-7s', proposal_kind),
            printf('%-18s', substr(target_recipe,1,18)),
            printf('%-15s', substr(target_node_id,1,15)),
            substr(rationale,1,80)
       FROM role_evolver_log $clause
      ORDER BY id DESC LIMIT 20;"
}

role_evolver_accept() {
  local id="${1:?id required}"
  sqlite3 "$STATE_DB" "UPDATE role_evolver_log SET status='accepted' WHERE id=$id;"
}
role_evolver_reject() {
  local id="${1:?id required}"
  sqlite3 "$STATE_DB" "UPDATE role_evolver_log SET status='rejected' WHERE id=$id;"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "role_evolver.sh — source me and call role_evolver_propose / list / accept / reject"
fi
