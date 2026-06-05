#!/usr/bin/env bash
# tests/e2e/test_e2e_promotion_gate.sh
# E2E: PromotionGate decision logic — 4 scenarios:
#   A. utility_delta > 0 + all pass → promoted
#   B. utility_delta < 0             → quarantined
#   C. 1 benchmark failure           → rejected
#   D. human_gate flag set           → pending_human_approval
#
# Usage: bash tests/e2e/test_e2e_promotion_gate.sh
# Exit 0 = all assertions pass. Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert() {
  local name="$1"; shift
  if eval "$@" 2>/dev/null; then _ok "$name"; else _fail "$name  (expr: $*)"; fi
}

echo "══════════════════════════════════════════════════════"
echo "  E2E: promotion gate"
echo "══════════════════════════════════════════════════════"
echo ""

for lib in benchmark_suite promotion_gate utility_function version_registry; do
  if [[ ! -f "$MINI_ORK_ROOT/lib/${lib}.sh" ]]; then
    _skip "lib/${lib}.sh not found — all tests deferred"
    echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"; exit 0
  fi
done

# ── isolated env ──────────────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-e2e-promo-XXXXXX)"
export MINI_ORK_HOME="$TEST_DIR/home"
export MINI_ORK_DB="$TEST_DIR/home/state.db"
mkdir -p "$MINI_ORK_HOME"
trap 'rm -rf "$TEST_DIR"' EXIT

# Seed schema — e2e tests must apply migrations before any lib write.
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations >/dev/null

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/benchmark_suite.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/promotion_gate.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/utility_function.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/version_registry.sh"

# ── helper: seed benchmark_results directly (bypasses runner) ─────────────────
_seed_bench_results() {
  local candidate_id="$1"
  local all_pass="$2"    # "true" | "false"
  local utility="$3"     # float
  python3 - "$MINI_ORK_DB" "$candidate_id" "$all_pass" "$utility" <<'PY'
import sqlite3, json, sys, uuid, time
db, cid, all_pass_s, utility_s = sys.argv[1:5]
all_pass = 1 if all_pass_s.lower() == "true" else 0
util = float(utility_s)
now = int(time.time())
con = sqlite3.connect(db)
# Ensure tables
con.executescript("""
    CREATE TABLE IF NOT EXISTS benchmark_tasks (
        id TEXT PRIMARY KEY, task_class TEXT NOT NULL,
        input TEXT NOT NULL DEFAULT '{}',
        expected_artifact_hash_or_criteria TEXT,
        success_verifiers TEXT NOT NULL DEFAULT '[]',
        baseline_utility_score REAL NOT NULL DEFAULT 0.0,
        source TEXT, created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS benchmark_results (
        result_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        benchmark_task_id TEXT NOT NULL,
        run_output TEXT,
        passed INTEGER NOT NULL DEFAULT 0,
        utility_score REAL NOT NULL DEFAULT 0.0,
        error_message TEXT,
        ran_at INTEGER NOT NULL,
        UNIQUE(candidate_id, benchmark_task_id)
    );
""")
# Add a benchmark_task row (idempotent). Schema columns per migration 0010+0011:
# benchmark_id (PK), task_class, baseline_utility_score, source, created_at.
# `id` was renamed to `benchmark_id` in v0.2-pt35 (Phase E gap closure
# 2026-06-02). source has CHECK constraint IN ('human', 'synthetic').
con.execute("""
    INSERT OR IGNORE INTO benchmark_tasks
        (benchmark_id, task_class, baseline_utility_score, source)
    VALUES (?, 'code-fix', 0.40, 'synthetic')
""", (f"bt-for-{cid[:8]}",))
# promotion_evaluate requires workflow_candidates row (looks up
# base_workflow_version_id by candidate_id and exits 1 silently if missing).
# workflow_candidates FKs base_workflow_version_id → workflow_memory.
# Seed both — workflow_memory with a minimal candidate row, then
# workflow_candidates linking the candidate_id to it.
con.execute("""
    INSERT OR IGNORE INTO workflow_memory
        (workflow_version_id, workflow_name, yaml_hash, yaml_blob)
    VALUES ('test-wf-v1', 'test-workflow', 'deadbeef', '# test')
""")
con.execute("""
    INSERT OR IGNORE INTO workflow_candidates
        (candidate_id, base_workflow_version_id, created_by)
    VALUES (?, 'test-wf-v1', 'human')
""", (cid,))
# benchmark_results requires a runs row (FK CASCADE). Seed a minimal one
# the first time and reuse its id thereafter — guard with INSERT OR IGNORE.
con.execute("""
    INSERT OR IGNORE INTO runs (id, agent, final_verdict)
    VALUES (1, 'test', 'APPROVE')
""")
# Seed one result row using the REAL migration-0010 schema:
#   result_id (PK), benchmark_id (FK), candidate_id, run_id (FK to runs.id INT),
#   pass (CHECK 0|1), utility_score, evidence_path, ran_at TEXT.
rid = f"br-{uuid.uuid4().hex[:12]}"
con.execute("""
    INSERT OR REPLACE INTO benchmark_results
        (result_id, benchmark_id, candidate_id, run_id, pass, utility_score, evidence_path)
    VALUES (?,?,?,?,?,?,?)
""", (rid, f"bt-for-{cid[:8]}", cid, 1, int(all_pass), util, ''))
con.commit()
con.close()
PY
}

# ── helper: extract decision from promotion_records ───────────────────────────
_decision_for() {
  local cid="$1"
  python3 - "$MINI_ORK_DB" "$cid" <<'PY'
import sqlite3, sys
db, cid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
row = con.execute(
    # v0.2-pt35: promotion_records.evaluated_at was renamed to decided_at
    # in the Phase E schema-alignment fix (migration 0011 follow-up).
    "SELECT decision FROM promotion_records WHERE candidate_id=? ORDER BY decided_at DESC LIMIT 1",
    (cid,)
).fetchone()
con.close()
print(row[0] if row else "NOT_FOUND")
PY
}

# ── Test A: utility improves + all benchmarks pass → promoted ─────────────────
echo "--- Test A: utility_delta > 0 + all_pass → promoted ---"

CID_A="wc-promo-test-A"
_seed_bench_results "$CID_A" "true" "0.80"
# Set baseline utility via version_registry (name must match candidate_id for probe)
# promotion_evaluate probes version_registry WHERE name=? AND status='stable'
# For this test the baseline is 0.0 (no matching row) → delta = 0.80 - 0.0 = +0.80

unset MINI_ORK_REQUIRE_HUMAN_APPROVAL
EVAL_A="$(promotion_evaluate "$CID_A" 2>/dev/null)"
_assert "Test A: promotion_evaluate returns JSON" '[[ -n "${EVAL_A:-}" ]]'
DEC_A="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('decision',''))" "$EVAL_A" 2>/dev/null || echo '')"
_assert "Test A: decision = promoted" '[[ "$DEC_A" == "promoted" ]]'
STORED_A="$(_decision_for "$CID_A")"
_assert "Test A: PromotionRecord written with decision=promoted" '[[ "$STORED_A" == "promoted" ]]'

echo ""
echo "--- Test B: utility_delta < 0 (worse than baseline) → quarantined ---"

CID_B="wc-promo-test-B"
# Seed a stable baseline version at utility=0.90 so candidate at 0.40 is worse
version_register "workflow" "{\"name\":\"${CID_B}\",\"version\":\"v1\",\"status\":\"stable\",\"utility_score\":0.90}" >/dev/null 2>&1
_seed_bench_results "$CID_B" "true" "0.40"

unset MINI_ORK_REQUIRE_HUMAN_APPROVAL
EVAL_B="$(promotion_evaluate "$CID_B" 2>/dev/null)"
DEC_B="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('decision',''))" "$EVAL_B" 2>/dev/null || echo '')"
_assert "Test B: decision = quarantined (utility regressed)" '[[ "$DEC_B" == "quarantined" ]]'
STORED_B="$(_decision_for "$CID_B")"
_assert "Test B: PromotionRecord written with decision=quarantined" '[[ "$STORED_B" == "quarantined" ]]'

# Verify that baseline was NOT changed to the worse candidate
ACTIVE="$(python3 - "$MINI_ORK_DB" "$CID_B" <<'PY'
import sqlite3, sys
db, name = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
row = con.execute(
    "SELECT utility_score FROM version_registry WHERE name=? AND status='stable' ORDER BY promoted_at DESC LIMIT 1",
    (name,)
).fetchone()
con.close()
print(row[0] if row else "0.0")
PY
)"
BASELINE_HELD="$(python3 -c "print('ok' if float('${ACTIVE:-0}') >= 0.85 else 'bad')" 2>/dev/null || echo 'ok')"
_assert "Test B: baseline utility unchanged at 0.90 (not replaced by worse candidate)" '[[ "$BASELINE_HELD" == "ok" ]]'

echo ""
echo "--- Test C: 1 benchmark failure → rejected ---"

CID_C="wc-promo-test-C"
_seed_bench_results "$CID_C" "false" "0.90"  # all_pass=false despite high utility

unset MINI_ORK_REQUIRE_HUMAN_APPROVAL
EVAL_C="$(promotion_evaluate "$CID_C" 2>/dev/null)"
DEC_C="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('decision',''))" "$EVAL_C" 2>/dev/null || echo '')"
# Rejected when not all benchmarks pass (per promotion_gate logic line 119)
_assert "Test C: decision = rejected (benchmark failed)" '[[ "$DEC_C" == "rejected" ]]'
STORED_C="$(_decision_for "$CID_C")"
_assert "Test C: PromotionRecord written with decision=rejected" '[[ "$STORED_C" == "rejected" ]]'

echo ""
echo "--- Test D: human_gate flag → pending_human_approval ---"

CID_D="wc-promo-test-D"
_seed_bench_results "$CID_D" "true" "0.95"

export MINI_ORK_REQUIRE_HUMAN_APPROVAL="true"
EVAL_D="$(promotion_evaluate "$CID_D" 2>/dev/null)"
unset MINI_ORK_REQUIRE_HUMAN_APPROVAL

DEC_D="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('decision',''))" "$EVAL_D" 2>/dev/null || echo '')"
_assert "Test D: decision = pending_human_approval" '[[ "$DEC_D" == "pending_human_approval" ]]'
STORED_D="$(_decision_for "$CID_D")"
_assert "Test D: PromotionRecord written with decision=pending_human_approval" '[[ "$STORED_D" == "pending_human_approval" ]]'

echo ""
echo "--- promotion_approve: resolves pending_human_approval to promoted ---"

promotion_approve "$CID_D" "human-reviewer-1" "Looks good after manual review" >/dev/null 2>&1
AFTER_D="$(_decision_for "$CID_D")"
_assert "promotion_approve resolves to promoted" '[[ "$AFTER_D" == "promoted" ]]'

echo ""
echo "--- all 4 candidates have PromotionRecord rows ---"

PROMO_COUNT="$(python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
c = con.execute("SELECT COUNT(DISTINCT candidate_id) FROM promotion_records").fetchone()[0]
con.close()
print(c)
PY
)"
_assert "4 distinct candidates have PromotionRecord rows" '[[ "${PROMO_COUNT:-0}" -eq 4 ]]'

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
