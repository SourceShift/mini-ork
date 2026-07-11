#!/usr/bin/env bash
# process_reward.sh — Process Reward Model (PRM) heuristic for mini-ork.
#
# Approximates a per-node reward 0.0-1.0 from observable trace signals. The
# weight table + verdict set are the SINGLE SOURCE OF TRUTH in
# mini_ork/learning/process_reward.py; both prm_score_trace and prm_backfill
# load them from there by file path (see _load_prm_weights) so the values
# cannot drift. Current outcome-dominant weights (status + review carry 0.80):
#   + W_STATUS   status = success                               (outcome)
#   + W_VERDICT  reviewer_verdict ∈ approve|approved|pass|success|ok,
#               gated on status == success (see verifiable-first note)
#   + W_TOOL     tool_calls non-empty (agent did work)           [capped]
#   + W_FILE     files_written or files_read non-empty (artifacts)[capped]
#   + W_DURATION duration_ms in [1000, 600000]
#   (W_COST retired to 0.00 — rewarded cost>0, near-constant/off-thesis)
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
# failed trace above 0.5. Same-family decontamination (zeroing the verdict
# term when agent_version_id contains opus/minimax/glm/kimi) has been
# REMOVED: the doer's lane is not a valid proxy for the reviewer's lane
# without a real reviewer_model column in execution_traces, and the
# asymmetric stripping of +0.15 from opus/minimax/glm/kimi runs biased
# GRPO group ranking. Same-family neutralization awaits a schema change.
# prm_score_trace and prm_backfill now load the weight table from the same
# canonical Python module (mini_ork/learning/process_reward.py) instead of
# each carrying an inline copy, so single-trace writes and bulk backfills can
# no longer diverge and silently break the router.
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
# v0.2-pt36 RLM-3: DB access now flows through the policy_store seam over
# lib/db_open.sh. STATE_DB is resolved per-call via $(mo_store_db_path) so
# MO_STORE_DB / MO_STORE_BACKEND=postgres can route without forking this lib.
# SQLite default (no env override) resolves to the same path as the old
# ${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db} convention. The PRM
# weight table is single-sourced from mini_ork/learning/process_reward.py
# (loaded by both prm_score_trace and prm_backfill); this refactor only
# changes the connect path, not the scoring math.
# shellcheck disable=SC1091
. "${MINI_ORK_ROOT}/lib/policy_store.sh"

prm_score_trace() {
  local trace_id="${1:?trace_id required}"
  mo_store_assert_sqlite
  local STATE_DB
  STATE_DB="$(mo_store_db_path)"
  python3 - "$STATE_DB" "$trace_id" "$MINI_ORK_ROOT" <<'PY'
import importlib.util, json, os, sqlite3, sys
db, trace_id, mo_root = sys.argv[1:4]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row
r = con.execute(
    "SELECT * FROM execution_traces WHERE trace_id=?", (trace_id,)
).fetchone()
if not r:
    con.close()
    sys.exit(0)

# Same-family decontamination removed: see FE note in the file header.
# Awaiting a real reviewer_model column in execution_traces.

def _len_json(s):
    try:
        v = json.loads(s or "[]")
        return len(v) if isinstance(v, (list, dict)) else 0
    except Exception:
        return 0

def _load_prm_weights(root):
    """Load the canonical PRM weight table + verdict set from the Python port
    by file path. Avoids `import mini_ork.*` so the facade in mini_ork/__init__
    is not executed; the target module is pure stdlib with no import-time IO."""
    src = os.path.join(root, "mini_ork", "learning", "process_reward.py")
    spec = importlib.util.spec_from_file_location("mo_prm_weights", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return (mod.W_STATUS, mod.W_TOOL, mod.W_FILE, mod.W_VERDICT,
            mod.W_DURATION, mod.ACTIVITY_CAP, set(mod._VERDICT_SET))

# ── PRM weight table — SINGLE SOURCE OF TRUTH ─────────────────────────────
# The weights + verdict set are canonically defined ONCE in
# mini_ork/learning/process_reward.py and loaded here by file path (no package
# import, so mini_ork/__init__'s facade is NOT triggered). prm_score_trace and
# prm_backfill BOTH load from the same module, so the two copies can no longer
# drift from each other or from the Python port. Parity is enforced by
# tests/unit/test_process_reward_parity.py.
W_STATUS, W_TOOL, W_FILE, W_VERDICT, W_DURATION, ACTIVITY_CAP, _VERDICT_SET = \
    _load_prm_weights(mo_root)
try:
    _activity_cap_enabled = int(os.environ.get("MO_PRM_ACTIVITY_CAP", "1")) != 0
except ValueError:
    _activity_cap_enabled = True

score = 0.0
status_success = (r["status"] or "") == "success"
# Goodhart fix: activity, timeliness, and verdict credit apply ONLY on success.
# A busy/timely FAILURE earns nothing (was ~0.25 from activity+duration) — this
# sharpens the success/failure separation the GRPO advantage learns from.
if status_success:
    score += W_STATUS
    tool_n = _len_json(r["tool_calls"])
    file_n = _len_json(r["files_written"]) + _len_json(r["files_read"])
    activity = (W_TOOL if tool_n > 0 else 0.0) + (W_FILE if file_n > 0 else 0.0)
    if _activity_cap_enabled:
        activity = min(activity, ACTIVITY_CAP)
    score += activity
    v = (r["reviewer_verdict"] or "").lower()
    if v in _VERDICT_SET:
        score += W_VERDICT
    dur = int(r["duration_ms"] or 0)
    if 1000 <= dur <= 600000:
        score += W_DURATION

score = round(min(1.0, max(0.0, score)), 4)
con.execute("UPDATE execution_traces SET process_reward=? WHERE trace_id=?",
            (score, trace_id))
con.commit()
con.close()
print(score)
sys.stderr.write(f"[prm] activity_cap={'on' if _activity_cap_enabled else 'off'}\n")
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
  mo_store_assert_sqlite
  local STATE_DB
  STATE_DB="$(mo_store_db_path)"
  python3 - "$STATE_DB" "$since" "$MINI_ORK_ROOT" <<'PY'
import importlib.util, json, os, sqlite3, sys, datetime
db, since_str, mo_root = sys.argv[1:4]
since = int(since_str)
since_iso = datetime.datetime.utcfromtimestamp(since).strftime('%Y-%m-%dT%H:%M:%S.000Z')

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT * FROM execution_traces WHERE created_at >= ?", (since_iso,)
).fetchall()

# Same-family decontamination removed: see FE note in the file header.
# Awaiting a real reviewer_model column in execution_traces.

def _len_json(s):
    try:
        v = json.loads(s or "[]")
        return len(v) if isinstance(v, (list, dict)) else 0
    except Exception:
        return 0

def _load_prm_weights(root):
    """Load the canonical PRM weight table + verdict set from the Python port
    by file path. Identical loader to prm_score_trace — both read the single
    source in mini_ork/learning/process_reward.py so they cannot diverge."""
    src = os.path.join(root, "mini_ork", "learning", "process_reward.py")
    spec = importlib.util.spec_from_file_location("mo_prm_weights", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return (mod.W_STATUS, mod.W_TOOL, mod.W_FILE, mod.W_VERDICT,
            mod.W_DURATION, mod.ACTIVITY_CAP, set(mod._VERDICT_SET))

# ── PRM weight table — SINGLE SOURCE OF TRUTH (see prm_score_trace) ────────
# Loaded from the same canonical module as prm_score_trace, so single-trace
# writes and bulk backfills compute identical process_reward for the same row.
W_STATUS, W_TOOL, W_FILE, W_VERDICT, W_DURATION, ACTIVITY_CAP, _VERDICT_SET = \
    _load_prm_weights(mo_root)
try:
    _activity_cap_enabled = int(os.environ.get("MO_PRM_ACTIVITY_CAP", "1")) != 0
except ValueError:
    _activity_cap_enabled = True

scored = 0
for r in rows:
    score = 0.0
    status_success = (r["status"] or "") == "success"
    # Goodhart fix (mirror of prm_score_trace): activity/timeliness/verdict credit
    # ONLY on success; a busy FAILURE scores 0. Keep identical to the single-trace
    # copy or single vs bulk process_reward diverge.
    if status_success:
        score += W_STATUS
        tool_n = _len_json(r["tool_calls"])
        file_n = _len_json(r["files_written"]) + _len_json(r["files_read"])
        activity = (W_TOOL if tool_n > 0 else 0.0) + (W_FILE if file_n > 0 else 0.0)
        if _activity_cap_enabled:
            activity = min(activity, ACTIVITY_CAP)
        score += activity
        v = (r["reviewer_verdict"] or "").lower()
        if v in _VERDICT_SET:
            score += W_VERDICT
        dur = int(r["duration_ms"] or 0)
        if 1000 <= dur <= 600000:
            score += W_DURATION
    score = round(min(1.0, max(0.0, score)), 4)
    con.execute("UPDATE execution_traces SET process_reward=? WHERE trace_id=?",
                (score, r["trace_id"]))
    scored += 1
con.commit()
con.close()
print(scored)
sys.stderr.write(f"[prm] activity_cap={'on' if _activity_cap_enabled else 'off'}\n")
PY
}

prm_low_scoring() {
  local task_class="${1:?task_class required}"
  local n="${2:-10}"
  mo_store_assert_sqlite
  local STATE_DB
  STATE_DB="$(mo_store_db_path)"
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
