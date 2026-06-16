#!/usr/bin/env bash
# rho_aggregator.sh — Retrospective Harness Optimization (RHO) win-rate
# aggregation. arXiv:2606.05922.
#
# Walks execution_traces, groups by (prompt_version_hash, task_class), and
# materialises wins / losses / ties per group into the prompt_win_rates
# table. A "win" is a trace whose final downstream outcome was a verifier
# pass; a "loss" is verifier failure (success status but reviewer_verdict
# in {REJECT, ESCALATE} OR status = failure); a "tie" is vacuous or running.
#
# Public API:
#   rho_aggregate_win_rates [--since EPOCH] [--task-class X]
#       Re-aggregate from execution_traces. Idempotent: upserts the row
#       keyed by (prompt_version_hash, task_class). Emits the row count
#       on stdout.
#
#   rho_top_prompts <task_class> <node_type> [<top_n>]
#       Print the top-N prompts by win_rate for the given task_class +
#       node_type, sample_size >= 3 to avoid one-shot noise.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

rho_aggregate_win_rates() {
  local since=0
  local class_filter=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --since)      since="$2"; shift 2 ;;
      --task-class) class_filter="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  python3 - "$STATE_DB" "$since" "$class_filter" <<'PY'
import sqlite3, sys, datetime
db, since_str, class_filter = sys.argv[1:4]
since = int(since_str)
since_iso = datetime.datetime.utcfromtimestamp(since).strftime('%Y-%m-%dT%H:%M:%S.000Z')

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")

clauses = ["created_at >= ?",
           "prompt_version_hash IS NOT NULL",
           "prompt_version_hash <> ''",
           "task_class IS NOT NULL",
           "task_class <> ''"]
params  = [since_iso]
if class_filter:
    clauses.append("task_class = ?")
    params.append(class_filter)

# Win/loss rubric:
#   win  : status='success' AND (reviewer_verdict IS NULL OR reviewer_verdict
#          NOT IN ('REJECT','ESCALATE','needs_revision'))
#   loss : status='failure'
#          OR (status='success' AND reviewer_verdict IN ('REJECT','ESCALATE','needs_revision'))
#   tie  : status IN ('running','vacuous','blocked','unknown') or other
sql = f"""
    SELECT prompt_version_hash, task_class,
        SUM(CASE
              WHEN status='success'
               AND (reviewer_verdict IS NULL
                    OR reviewer_verdict NOT IN ('REJECT','ESCALATE','needs_revision'))
              THEN 1 ELSE 0 END) AS wins,
        SUM(CASE
              WHEN status='failure'
                OR (status='success' AND reviewer_verdict IN ('REJECT','ESCALATE','needs_revision'))
              THEN 1 ELSE 0 END) AS losses,
        SUM(CASE
              WHEN status IN ('running','vacuous','blocked','unknown')
                   OR status IS NULL
              THEN 1 ELSE 0 END) AS ties
      FROM execution_traces
     WHERE {' AND '.join(clauses)}
     GROUP BY prompt_version_hash, task_class
"""
rows = con.execute(sql, params).fetchall()

updated = 0
for prompt, tc, wins, losses, ties in rows:
    n = (wins or 0) + (losses or 0) + (ties or 0)
    wr = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0
    con.execute("""
        INSERT INTO prompt_win_rates
            (prompt_version_hash, task_class, wins, losses, ties,
             win_rate, sample_size,
             last_updated)
        VALUES (?,?,?,?,?,?,?,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(prompt_version_hash, task_class) DO UPDATE SET
            wins=excluded.wins, losses=excluded.losses, ties=excluded.ties,
            win_rate=excluded.win_rate, sample_size=excluded.sample_size,
            last_updated=excluded.last_updated
    """, (prompt, tc, wins or 0, losses or 0, ties or 0,
          round(wr, 4), n))
    updated += 1

con.commit()
con.close()
print(updated)
PY
}

rho_top_prompts() {
  local task_class="${1:?task_class required}"
  local node_type="${2:-}"
  local top_n="${3:-5}"
  local where="task_class='$task_class' AND sample_size >= 3"
  [ -n "$node_type" ] && where="$where AND (node_type IS NULL OR node_type='$node_type')"
  sqlite3 -separator ' | ' "$STATE_DB" \
    "SELECT printf('%.3f', win_rate),
            printf('%4d', sample_size),
            printf('%-12s', substr(prompt_version_hash,1,12)),
            COALESCE(node_type,'?')
       FROM prompt_win_rates
      WHERE $where
      ORDER BY win_rate DESC, sample_size DESC
      LIMIT $top_n;"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "rho_aggregator.sh — source me and call rho_aggregate_win_rates [--since EPOCH] [--task-class X] / rho_top_prompts <class> [<node_type>] [<top_n>]"
fi
