#!/usr/bin/env bash
# process_reward.sh — Process Reward Model (PRM) heuristic for mini-ork.
#
# Approximates a per-node reward 0.0-1.0 from observable trace signals:
#   + 0.40  status = success
#   + 0.20  tool_calls non-empty (agent actually did work)        [capped, see below]
#   + 0.10  files_written or files_read non-empty (artifacts)      [capped, see below]
#   + 0.15  reviewer_verdict in {APPROVE, pass, success, ok}, gated on
#           status == success AND not same-family (see verifiable-first note)
#   + 0.10  duration_ms in [1000, 600000] (not too fast, not too slow)
#   + 0.05  cost_usd > 0 (real LLM invocation, not a stub)
# Total maxes at 1.0, floors at 0.0; partial credit is the point.
#
# Activity cap (Goodhart guard): by default the combined contribution of
# tool_calls + files_read/written is clamped at ACTIVITY_CAP=0.15 so that a
# noisy failed trace with high activity cannot outscore a clean bare-success
# trace. Without this clamp, status=failed + tool_calls=5 + files=2 + cost>0
# lands at 0.45 while status=success with no work lands at 0.40 — i.e. the
# "do more bad work" path wins. Set MO_PRM_ACTIVITY_CAP=0 to reproduce the
# legacy uncapped weighting for A/B comparison.
#
# Verifiable-first (FE): the +0.15 reviewer_verdict term is gated behind
# status == success so an adversarial / noisy LLM judge cannot lift a
# failed trace above 0.5. Same-family traces (agent_version_id contains
# opus / minimax / glm / kimi) get the verdict term zeroed to neutralize
# judge self-preference — the execution_traces schema has no reviewer_model
# column, so the doer's lane is the conservative proxy. PRM copies in
# prm_score_trace and prm_backfill MUST stay behaviorally identical: the
# weight table below is mirrored verbatim in prm_backfill. Drift between
# the two causes process_reward to diverge between single-trace writes and
# bulk backfills, silently breaking the router.
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
import json, os, sqlite3, sys
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

# Verifiable-first: same-family detection uses agent_version_id family token
# as the conservative proxy for "reviewer and doer share a family". The
# execution_traces schema has no reviewer_model column, so any agent_version_id
# that encodes a known single-family lane (opus/minimax/glm/kimi) triggers
# neutralization of the +0.15 verdict term. Missing or non-matching ids keep
# the verdict credit on — better to risk false-positive judge influence than
# to over-neutralize cross-family lanes.
_FAMILY_TOKENS = ("opus", "minimax", "glm", "kimi")
def _same_family(agent_version_id):
    if not agent_version_id:
        return False
    av = str(agent_version_id).lower()
    return any(tok in av for tok in _FAMILY_TOKENS)

def _len_json(s):
    try:
        v = json.loads(s or "[]")
        return len(v) if isinstance(v, (list, dict)) else 0
    except Exception:
        return 0

# ── PRM weight table (DO NOT EDIT THIS COPY WITHOUT EDITING prm_backfill) ──
# This table is mirrored verbatim in prm_backfill below. Both copies MUST
# stay byte-identical so single-trace writes (prm_score_trace) and bulk
# backfills (prm_backfill) compute the same process_reward for the same row.
#   W_STATUS       = 0.40   # status == "success"
#   W_TOOL         = 0.20   # tool_calls non-empty
#   W_FILE         = 0.10   # files_read or files_written non-empty
#   W_VERDICT      = 0.15   # reviewer_verdict ∈ approve|approved|pass|success|ok
#                           #   AND status==success AND not same-family
#   W_DURATION     = 0.10   # 1000 <= duration_ms <= 600000
#   W_COST         = 0.05   # cost_usd > 0
#   ACTIVITY_CAP   = 0.15   # MO_PRM_ACTIVITY_CAP=1 (default): clamp W_TOOL+W_FILE
#                           # MO_PRM_ACTIVITY_CAP=0 (legacy): no clamp, activity can dominate
W_STATUS, W_TOOL, W_FILE, W_VERDICT, W_DURATION, W_COST, ACTIVITY_CAP = \
    0.40, 0.20, 0.10, 0.15, 0.10, 0.05, 0.15
try:
    _activity_cap_enabled = int(os.environ.get("MO_PRM_ACTIVITY_CAP", "1")) != 0
except ValueError:
    _activity_cap_enabled = True

score = 0.0
status_success = (r["status"] or "") == "success"
if status_success:
    score += W_STATUS
tool_n = _len_json(r["tool_calls"])
file_n = _len_json(r["files_written"]) + _len_json(r["files_read"])
activity = (W_TOOL if tool_n > 0 else 0.0) + (W_FILE if file_n > 0 else 0.0)
# Goodhart guard: cap combined activity contribution so noisy failed work
# cannot outscore bare success. Legacy uncapped behavior is opt-in via
# MO_PRM_ACTIVITY_CAP=0.
if _activity_cap_enabled:
    activity = min(activity, ACTIVITY_CAP)
score += activity
v = (r["reviewer_verdict"] or "").lower()
# Verdict term GATED on status==success AND NOT same-family. A failed trace
# cannot be lifted by an approving judge; same-family judges contribute
# nothing so they cannot self-prefer.
if status_success and v in {"approve", "approved", "pass", "success", "ok"} \
        and not _same_family(r["agent_version_id"]):
    score += W_VERDICT
dur = int(r["duration_ms"] or 0)
if 1000 <= dur <= 600000:
    score += W_DURATION
if float(r["cost_usd"] or 0) > 0:
    score += W_COST

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
import json, os, sqlite3, sys, datetime
db, since_str = sys.argv[1:3]
since = int(since_str)
since_iso = datetime.datetime.utcfromtimestamp(since).strftime('%Y-%m-%dT%H:%M:%S.000Z')

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT * FROM execution_traces WHERE created_at >= ?", (since_iso,)
).fetchall()

# Verifiable-first: same-family detection — must match prm_score_trace byte
# for byte to keep the two PRM heuristic copies behaviorally identical.
_FAMILY_TOKENS = ("opus", "minimax", "glm", "kimi")
def _same_family(agent_version_id):
    if not agent_version_id:
        return False
    av = str(agent_version_id).lower()
    return any(tok in av for tok in _FAMILY_TOKENS)

def _len_json(s):
    try:
        v = json.loads(s or "[]")
        return len(v) if isinstance(v, (list, dict)) else 0
    except Exception:
        return 0

# ── PRM weight table (DO NOT EDIT THIS COPY WITHOUT EDITING prm_score_trace) ──
# Mirror copy of prm_score_trace's weight table. Both copies MUST stay
# byte-identical so single-trace writes and bulk backfills compute the same
# process_reward for the same row.
#   W_STATUS       = 0.40   # status == "success"
#   W_TOOL         = 0.20   # tool_calls non-empty
#   W_FILE         = 0.10   # files_read or files_written non-empty
#   W_VERDICT      = 0.15   # reviewer_verdict ∈ approve|approved|pass|success|ok
#                           #   AND status==success AND not same-family
#   W_DURATION     = 0.10   # 1000 <= duration_ms <= 600000
#   W_COST         = 0.05   # cost_usd > 0
#   ACTIVITY_CAP   = 0.15   # MO_PRM_ACTIVITY_CAP=1 (default): clamp W_TOOL+W_FILE
#                           # MO_PRM_ACTIVITY_CAP=0 (legacy): no clamp, activity can dominate
W_STATUS, W_TOOL, W_FILE, W_VERDICT, W_DURATION, W_COST, ACTIVITY_CAP = \
    0.40, 0.20, 0.10, 0.15, 0.10, 0.05, 0.15
try:
    _activity_cap_enabled = int(os.environ.get("MO_PRM_ACTIVITY_CAP", "1")) != 0
except ValueError:
    _activity_cap_enabled = True

scored = 0
for r in rows:
    score = 0.0
    status_success = (r["status"] or "") == "success"
    if status_success:
        score += W_STATUS
    tool_n = _len_json(r["tool_calls"])
    file_n = _len_json(r["files_written"]) + _len_json(r["files_read"])
    activity = (W_TOOL if tool_n > 0 else 0.0) + (W_FILE if file_n > 0 else 0.0)
    # Goodhart guard: cap combined activity contribution so noisy failed work
    # cannot outscore bare success. Legacy uncapped behavior is opt-in via
    # MO_PRM_ACTIVITY_CAP=0.
    if _activity_cap_enabled:
        activity = min(activity, ACTIVITY_CAP)
    score += activity
    v = (r["reviewer_verdict"] or "").lower()
    # Verdict term GATED on status==success AND NOT same-family. Identical to
    # prm_score_trace; do not drift these two copies or process_reward will
    # diverge between single-trace and bulk paths.
    if status_success and v in {"approve", "approved", "pass", "success", "ok"} \
            and not _same_family(r["agent_version_id"]):
        score += W_VERDICT
    dur = int(r["duration_ms"] or 0)
    if 1000 <= dur <= 600000:
        score += W_DURATION
    if float(r["cost_usd"] or 0) > 0:
        score += W_COST
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
