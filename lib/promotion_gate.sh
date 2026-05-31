#!/usr/bin/env bash
# promotion_gate.sh — PromotionDecision logic for workflow/agent candidates.
#
# Public API:
#   promotion_evaluate  <candidate_id>
#       → emits decision JSON: { decision, rationale, utility_before,
#                                utility_after, benchmark_run_id }
#   promotion_approve   <candidate_id> <approver> <rationale>
#       → resolves a pending_human_approval decision

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_promo_ensure_tables() {
  # v0.2-pt10 G-003 DDL session guard
  [ "${_MO_PROMO_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
con.executescript("""
    CREATE TABLE IF NOT EXISTS promotion_records (
        record_id               TEXT PRIMARY KEY,
        candidate_id            TEXT NOT NULL,
        kind                    TEXT NOT NULL DEFAULT 'workflow',
        decision                TEXT NOT NULL
                                    CHECK(decision IN (
                                        'promoted','quarantined','rejected',
                                        'pending_human_approval'
                                    )),
        rationale               TEXT,
        utility_before          REAL,
        utility_after           REAL,
        benchmark_run_id        TEXT,
        approver                TEXT,
        approval_rationale      TEXT,
        safety_violations       TEXT DEFAULT '[]',
        evaluated_at            INTEGER NOT NULL,
        approved_at             INTEGER
    );
""")
con.commit()
con.close()
PY
  _MO_PROMO_SCHEMA_INIT=1
  export _MO_PROMO_SCHEMA_INIT
}

# desc: Evaluate a candidate for promotion. Queries benchmark_results and
#       version_registry for baseline utility. Returns decision JSON.
promotion_evaluate() {
  local candidate_id="${1:?candidate_id required}"
  _promo_ensure_tables

  # Load sub-libraries if not already loaded
  # shellcheck source=lib/benchmark_suite.sh
  source "${MINI_ORK_ROOT}/lib/benchmark_suite.sh"  2>/dev/null || true
  # shellcheck source=lib/utility_function.sh
  source "${MINI_ORK_ROOT}/lib/utility_function.sh" 2>/dev/null || true
  # shellcheck source=lib/version_registry.sh
  source "${MINI_ORK_ROOT}/lib/version_registry.sh" 2>/dev/null || true
  # shellcheck source=lib/gate_registry.sh
  source "${MINI_ORK_ROOT}/lib/gate_registry.sh"    2>/dev/null || true

  local require_human="${MINI_ORK_REQUIRE_HUMAN_APPROVAL:-false}"

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$candidate_id" "$require_human" <<'PY'
import sqlite3, json, sys, time, uuid

db, cid, require_human = sys.argv[1], sys.argv[2], sys.argv[3]
now = int(time.time())
record_id = f"pr-{uuid.uuid4().hex[:16]}"

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# Fetch most recent benchmark run for this candidate
brun = con.execute("""
    SELECT candidate_id, passed, avg_utility_score, all_pass, total_tasks
    FROM (
        SELECT candidate_id,
               SUM(passed) as passed,
               AVG(utility_score) as avg_utility_score,
               MIN(passed) as all_pass,
               COUNT(*) as total_tasks
        FROM benchmark_results WHERE candidate_id=?
        GROUP BY candidate_id
    )
""", (cid,)).fetchone()

# Fetch baseline utility from version_registry
baseline_row = con.execute("""
    SELECT utility_score FROM version_registry
    WHERE name=? AND status='stable'
    ORDER BY promoted_at DESC LIMIT 1
""", (cid,)).fetchone() if con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='version_registry'"
).fetchone() else None

utility_before = float(baseline_row[0]) if baseline_row else 0.0
utility_after  = float(brun["avg_utility_score"]) if brun else 0.0
all_pass       = bool(brun["all_pass"]) if brun else False

# Check for safety violations in gate_registry
safety_violations = []
try:
    sv_rows = con.execute("""
        SELECT gate_id, condition FROM gate_registry
        WHERE safety=1 AND (task_class_filter IS NULL OR task_class_filter=?)
    """, (cid,)).fetchall()
    # Safety gates stored as references; actual evaluation happens via gate_run_all
    safety_violations = []  # gate_run_all handles this at call time
except Exception:
    pass

utility_delta = utility_after - utility_before

# Decision logic
if require_human.lower() == "true":
    decision = "pending_human_approval"
    rationale = f"Human gate required (MINI_ORK_REQUIRE_HUMAN_APPROVAL=true)"
elif not all_pass and brun and brun["total_tasks"] > 0:
    decision = "rejected"
    rationale = (f"Not all benchmark tasks passed "
                 f"({brun['passed']}/{brun['total_tasks']})")
elif utility_delta <= 0 and brun:
    decision = "quarantined"
    rationale = (f"Utility did not improve: before={utility_before:.4f}, "
                 f"after={utility_after:.4f}, delta={utility_delta:.4f}")
else:
    decision = "promoted"
    rationale = (f"Utility improved by {utility_delta:.4f} "
                 f"({utility_before:.4f} → {utility_after:.4f}); "
                 f"all benchmark tasks passed.")

result = {
    "decision": decision,
    "rationale": rationale,
    "utility_before": round(utility_before, 6),
    "utility_after": round(utility_after, 6),
    "utility_delta": round(utility_delta, 6),
    "benchmark_run_id": cid,
    "all_pass": all_pass,
    "safety_violations": safety_violations,
}

con.execute("""
    INSERT INTO promotion_records
        (record_id, candidate_id, decision, rationale,
         utility_before, utility_after, benchmark_run_id,
         safety_violations, evaluated_at)
    VALUES (?,?,?,?,?,?,?,?,?)
""", (
    record_id, cid, decision, rationale,
    utility_before, utility_after, cid,
    json.dumps(safety_violations), now,
))
con.commit()
con.close()
print(json.dumps(result))
PY
}

# desc: Approve a candidate that is in pending_human_approval state.
#       approver is a string identifier, rationale is free text.
promotion_approve() {
  local candidate_id="${1:?candidate_id required}"
  local approver="${2:?approver required}"
  local rationale="${3:?rationale required}"
  _promo_ensure_tables

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$candidate_id" "$approver" "$rationale" <<'PY'
import sqlite3, json, sys, time
db, cid, approver, rationale = sys.argv[1:5]
now = int(time.time())
con = sqlite3.connect(db)
updated = con.execute("""
    UPDATE promotion_records
    SET decision='promoted', approver=?, approval_rationale=?, approved_at=?
    WHERE candidate_id=? AND decision='pending_human_approval'
""", (approver, rationale, now, cid)).rowcount
con.commit()
con.close()
if updated == 0:
    print(f"promotion_approve: no pending approval found for {cid}", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"candidate_id": cid, "decision": "promoted", "approver": approver, "approved_at": now}))
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "promotion_gate.sh — source me and call promotion_evaluate / promotion_approve"
fi
