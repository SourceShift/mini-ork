#!/usr/bin/env bash
# process_reward.sh — Process Reward Model (PRM) heuristic for mini-ork.
#
# Approximates a per-node reward 0.0-1.0 from observable trace signals:
#   + 0.40  status = success
#   + 0.20  tool_calls non-empty (agent actually did work)
#   + 0.10  files_written or files_read non-empty (artifacts produced or read)
#   + 0.15  reviewer_verdict in {APPROVE, pass, success, ok}
#   + 0.10  duration_ms in [1000, 600000] (not too fast, not too slow)
#   + 0.05  cost_usd > 0 (real LLM invocation, not a stub)
# Total maxes at 1.0, floors at 0.0; partial credit is the point.
#
# Public API:
#   prm_score_trace   <trace_id>        compute + UPDATE process_reward
#   prm_backfill      [--since EPOCH]   bulk-score every recent trace
#   prm_low_scoring   <task_class> N    print N traces with reward < 0.5
#
# Pure SQL + Python stdlib; no LLM dispatch. The full PRM literature uses
# a learned process reward model — that's a v2 upgrade. v1 covers the
# 80% of clear cases (no files touched, no tool calls, vacuous status).

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

prm_score_trace() {
  local trace_id="${1:?trace_id required}"
  python3 - "$STATE_DB" "$trace_id" <<'PY'
import json, sqlite3, sys
db, trace_id = sys.argv[1:3]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row
r = con.execute(
    "SELECT * FROM execution_traces WHERE trace_id=?", (trace_id,)
).fetchone()
if not r:
    con.close()
    sys.exit(0)

def _len_json(s):
    try:
        v = json.loads(s or "[]")
        return len(v) if isinstance(v, (list, dict)) else 0
    except Exception:
        return 0

score = 0.0
if (r["status"] or "") == "success":
    score += 0.40
if _len_json(r["tool_calls"]) > 0:
    score += 0.20
if _len_json(r["files_written"]) > 0 or _len_json(r["files_read"]) > 0:
    score += 0.10
v = (r["reviewer_verdict"] or "").lower()
if v in {"approve", "approved", "pass", "success", "ok"}:
    score += 0.15
dur = int(r["duration_ms"] or 0)
if 1000 <= dur <= 600000:
    score += 0.10
if float(r["cost_usd"] or 0) > 0:
    score += 0.05

score = round(min(1.0, max(0.0, score)), 4)
con.execute("UPDATE execution_traces SET process_reward=? WHERE trace_id=?",
            (score, trace_id))
con.commit()
con.close()
print(score)
PY
}

prm_backfill() {
  local since=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --since) since="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  python3 - "$STATE_DB" "$since" <<'PY'
import json, sqlite3, sys, datetime
db, since_str = sys.argv[1:3]
since = int(since_str)
since_iso = datetime.datetime.utcfromtimestamp(since).strftime('%Y-%m-%dT%H:%M:%S.000Z')

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT * FROM execution_traces WHERE created_at >= ?", (since_iso,)
).fetchall()

def _len_json(s):
    try:
        v = json.loads(s or "[]")
        return len(v) if isinstance(v, (list, dict)) else 0
    except Exception:
        return 0

scored = 0
for r in rows:
    score = 0.0
    if (r["status"] or "") == "success":
        score += 0.40
    if _len_json(r["tool_calls"]) > 0:
        score += 0.20
    if _len_json(r["files_written"]) > 0 or _len_json(r["files_read"]) > 0:
        score += 0.10
    v = (r["reviewer_verdict"] or "").lower()
    if v in {"approve", "approved", "pass", "success", "ok"}:
        score += 0.15
    dur = int(r["duration_ms"] or 0)
    if 1000 <= dur <= 600000:
        score += 0.10
    if float(r["cost_usd"] or 0) > 0:
        score += 0.05
    score = round(min(1.0, max(0.0, score)), 4)
    con.execute("UPDATE execution_traces SET process_reward=? WHERE trace_id=?",
                (score, r["trace_id"]))
    scored += 1
con.commit()
con.close()
print(scored)
PY
}

prm_low_scoring() {
  local task_class="${1:?task_class required}"
  local n="${2:-10}"
  sqlite3 -separator ' | ' "$STATE_DB" \
    "SELECT printf('%.2f', process_reward),
            printf('%-15s', status),
            substr(trace_id,1,20)
       FROM execution_traces
      WHERE task_class='$task_class' AND process_reward IS NOT NULL
      ORDER BY process_reward ASC
      LIMIT $n;"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "process_reward.sh — source me and call prm_score_trace / prm_backfill / prm_low_scoring"
fi
