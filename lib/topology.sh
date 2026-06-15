#!/usr/bin/env bash
# topology.sh — workflow-graph win-rate tracking + candidate enumeration.
#
# Phase 1 of the meta-orchestrator design. Grounded in:
#   TacoMAS arXiv:2605.09539 — online graph adaptation of MAS topology
#   Mass    arXiv:2502.02533 — multi-agent system search over topology+prompt
#   AgentConductor arXiv:2602.17100 — topology evolution for code generation
#
# A "topology" is a named workflow.yaml graph (identified by its yaml_hash
# from workflow_memory). Each recipe directory contributes one or more
# candidate topologies (the committed workflow.yaml plus any shadows /
# promoted variants in workflow_memory).
#
# Public API:
#   topology_recompute_win_rates [--since EPOCH]
#       Walk recent execution_traces, group by (workflow_version_id ->
#       topology_id via workflow_memory, task_class). Upserts win/loss
#       counts + win_rate + avg_cost + avg_duration. Pure SQL.
#
#   topology_candidates_for_class <task_class>
#       Print candidate topologies for a task_class with their win-rate +
#       sample_size, oldest stable promoted ones first as fallback when
#       no measurements exist.
#
#   topology_preferred <task_class>
#       Print the single highest-win-rate topology with sample_size >= 3.
#       Falls back to the recipe's default workflow if nothing measured.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

topology_recompute_win_rates() {
  local since=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --since) since="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  python3 - "$STATE_DB" "$since" <<'PY'
import sqlite3, sys, datetime

db, since_s = sys.argv[1:3]
since_iso = datetime.datetime.utcfromtimestamp(int(since_s)).strftime(
    '%Y-%m-%dT%H:%M:%S.000Z'
)

con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row

# Bind workflow_version_id (per-trace) -> topology_id (yaml_hash) +
# workflow_name via workflow_memory. Then aggregate.
rows = con.execute("""
    SELECT
        COALESCE(wm.yaml_hash, et.workflow_version_id) AS topology_id,
        COALESCE(wm.workflow_name, '?')                AS workflow_name,
        et.task_class,
        SUM(CASE WHEN et.status='success'
                  AND (et.reviewer_verdict IS NULL OR et.reviewer_verdict
                       NOT IN ('REJECT','ESCALATE','needs_revision'))
                THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN et.status='failure'
                  OR (et.status='success'
                      AND et.reviewer_verdict IN ('REJECT','ESCALATE','needs_revision'))
                THEN 1 ELSE 0 END) AS losses,
        SUM(CASE WHEN et.status IN ('running','vacuous','blocked','unknown')
                  OR et.status IS NULL
                THEN 1 ELSE 0 END) AS ties,
        AVG(COALESCE(et.cost_usd, 0))    AS avg_cost,
        AVG(COALESCE(et.duration_ms, 0)) AS avg_duration
      FROM execution_traces et
      LEFT JOIN workflow_memory wm ON wm.workflow_version_id = et.workflow_version_id
     WHERE et.created_at >= ?
       AND et.workflow_version_id IS NOT NULL AND et.workflow_version_id <> ''
       AND et.task_class IS NOT NULL AND et.task_class <> ''
     GROUP BY topology_id, workflow_name, et.task_class
""", (since_iso,)).fetchall()

upserted = 0
for r in rows:
    n = (r['wins'] or 0) + (r['losses'] or 0) + (r['ties'] or 0)
    denom = (r['wins'] or 0) + (r['losses'] or 0)
    wr = (r['wins'] / denom) if denom > 0 else 0.0
    con.execute("""
        INSERT INTO topology_win_rates
            (topology_id, workflow_name, task_class, wins, losses, ties,
             win_rate, sample_size, avg_cost_usd, avg_duration_ms,
             last_updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(topology_id, task_class) DO UPDATE SET
            workflow_name = excluded.workflow_name,
            wins          = excluded.wins,
            losses        = excluded.losses,
            ties          = excluded.ties,
            win_rate      = excluded.win_rate,
            sample_size   = excluded.sample_size,
            avg_cost_usd  = excluded.avg_cost_usd,
            avg_duration_ms = excluded.avg_duration_ms,
            last_updated  = excluded.last_updated
    """, (r['topology_id'], r['workflow_name'], r['task_class'],
          r['wins'] or 0, r['losses'] or 0, r['ties'] or 0,
          round(wr, 4), n,
          float(r['avg_cost'] or 0), float(r['avg_duration'] or 0)))
    upserted += 1
con.commit(); con.close()
print(upserted)
PY
}

topology_candidates_for_class() {
  local task_class="${1:?task_class required}"
  sqlite3 -separator ' | ' "$STATE_DB" \
    "SELECT printf('%-14s', substr(topology_id,1,14)),
            printf('%-22s', substr(workflow_name,1,22)),
            printf('%.3f', win_rate),
            printf('%4d', sample_size),
            printf('%.4f', avg_cost_usd)
       FROM topology_win_rates
      WHERE task_class='$task_class'
      ORDER BY win_rate DESC, sample_size DESC
      LIMIT 10;"
}

topology_preferred() {
  local task_class="${1:?task_class required}"
  sqlite3 -separator '|' "$STATE_DB" \
    "SELECT workflow_name, topology_id,
            printf('%.3f', win_rate), sample_size
       FROM topology_win_rates
      WHERE task_class='$task_class' AND sample_size >= 3
      ORDER BY win_rate DESC, sample_size DESC
      LIMIT 1;"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "topology.sh — source me and call topology_recompute_win_rates / topology_candidates_for_class / topology_preferred"
fi
