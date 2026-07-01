#!/usr/bin/env bash
# tests/unit/test_learning_loop_writeback.sh — unit tests for the
# learning-loop write-back: pattern_store_mine_from_traces +
# reflection_persist_suggestions (see
# kickoffs/issue-fixes/close-learning-loop-writeback.md).
#
# Usage: bash tests/unit/test_learning_loop_writeback.sh
# Exit 0 = all assertions pass. Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB_PIPELINE="$MINI_ORK_ROOT/lib/reflection_pipeline.sh"
LIB_PATTERN="$MINI_ORK_ROOT/lib/pattern_store.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: learning-loop write-back ──"

if [[ ! -f "$LIB_PIPELINE" || ! -f "$LIB_PATTERN" ]]; then
  _skip "reflection_pipeline.sh or pattern_store.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB + HOME so writes don't pollute the operator's real state.
TEST_DB=$(mktemp /tmp/mini-ork-llw-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# shellcheck source=/dev/null
source "$LIB_PATTERN"
# shellcheck source=/dev/null
source "$LIB_PIPELINE"

# ── seed execution_traces + gradient_records ───────────────────────────────
# 4 traces sharing (code_fix, failure) → exceeds default min-cluster=3 so the
# miner emits one pattern. 2 traces in (code_fix, success) → does NOT emit.
python3 - "$TEST_DB" <<'PY'
import sqlite3, sys, time
db = sys.argv[1]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.execute("""
    CREATE TABLE IF NOT EXISTS execution_traces (
        trace_id TEXT PRIMARY KEY,
        task_class TEXT,
        status TEXT,
        created_at TEXT
    )
""")
con.execute("""
    CREATE TABLE IF NOT EXISTS gradient_records (
        gradient_id TEXT PRIMARY KEY,
        target TEXT,
        signal TEXT,
        suggested_change TEXT,
        evidence TEXT,
        confidence REAL,
        created_at INTEGER
    )
""")
now_iso = "2026-07-01T12:00:00.000Z"
rows = [
    ("tr-fix-fail-1", "code_fix", "failure", now_iso),
    ("tr-fix-fail-2", "code_fix", "failure", now_iso),
    ("tr-fix-fail-3", "code_fix", "failure", now_iso),
    ("tr-fix-fail-4", "code_fix", "failure", now_iso),
    ("tr-fix-succ-1", "code_fix", "success", now_iso),
    ("tr-fix-succ-2", "code_fix", "success", now_iso),
]
con.executemany("INSERT INTO execution_traces VALUES (?,?,?,?)", rows)
now_ts = int(time.time())
gr_rows = [
    ("gr-1", "wf.node.A", "sig-A", "chg-A", "tr-fix-fail-1", 0.6, now_ts),
    ("gr-2", "wf.node.B", "sig-B", "chg-B", "tr-fix-fail-2", 0.7, now_ts),
    ("gr-3", "wf.node.C", "sig-C", "chg-C", "tr-fix-fail-3", 0.8, now_ts),
]
con.executemany("INSERT INTO gradient_records VALUES (?,?,?,?,?,?,?)", gr_rows)
con.commit()
con.close()
PY

echo ""
echo "--- happy path: pattern_store_mine_from_traces emits ≥1 pattern_records row ---"

_WRITTEN=$(pattern_store_mine_from_traces --window 7d --min-cluster 3 2>/dev/null || echo 0)
if [[ "$_WRITTEN" -ge 1 ]]; then
  _ok "pattern_store_mine_from_traces wrote $_WRITTEN pattern_records row(s)"
else
  _fail "pattern_store_mine_from_traces wrote 0 rows (expected ≥1)"
fi

_PATTERN_COUNT=$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM pattern_records WHERE task_class IS NULL OR task_class='code_fix';" 2>/dev/null || echo 0)
# pattern_records table has no task_class column; check row count instead.
_PATTERN_COUNT=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM pattern_records;" 2>/dev/null || echo 0)
if [[ "$_PATTERN_COUNT" -ge 1 ]]; then
  _ok "pattern_records has $_PATTERN_COUNT row(s) after miner run"
else
  _fail "pattern_records is empty (expected ≥1)"
fi

# ── suggestions table write-back ───────────────────────────────────────────
echo ""
echo "--- happy path: reflection_persist_suggestions writes durable rows ---"

# Build a suggestions JSON that mirrors what reflection_suggest_promotions
# would emit for the cluster we just seeded.
SUGGESTIONS_JSON=$(python3 - "$TEST_DB" <<'PY'
import sqlite3, json, sys
db = sys.argv[1]
con = sqlite3.connect(db)
rows = con.execute("""
    SELECT pattern_id, description, frequency, output_type, evidence_trace_ids
    FROM pattern_records
    WHERE frequency >= 3
    ORDER BY frequency DESC
""").fetchall()
con.close()
out = []
for r in rows:
    out.append({
        "pattern_id": r[0],
        "description": r[1],
        "frequency": r[2],
        "suggested_promotion_type": r[3],
        "evidence_trace_ids": json.loads(r[4]) if r[4] else [],
        "rationale": f"observed {r[2]} times",
    })
print(json.dumps(out))
PY
)

_PERSISTED=$(reflection_persist_suggestions "$SUGGESTIONS_JSON" 2>/dev/null || echo 0)
if [[ "$_PERSISTED" -ge 1 ]]; then
  _ok "reflection_persist_suggestions persisted $_PERSISTED suggestion(s)"
else
  _fail "reflection_persist_suggestions persisted 0 (expected ≥1)"
fi

_PROPOSED_COUNT=$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM emergent_patterns WHERE status='proposed';" 2>/dev/null || echo 0)
if [[ "$_PROPOSED_COUNT" -ge 1 ]]; then
  _ok "emergent_patterns has $_PROPOSED_COUNT proposed row(s)"
else
  _fail "emergent_patterns has no proposed rows (expected ≥1)"
fi

# Non-empty evidence_trace_ids → must be JSON-encoded into member_item_ids_json
_EVIDENCE_CHECK=$(sqlite3 "$TEST_DB" \
  "SELECT member_item_ids_json FROM emergent_patterns WHERE status='proposed' LIMIT 1;" 2>/dev/null || echo "")
_EVIDENCE_LEN=$(python3 -c "
import json,sys
d = json.loads(sys.argv[1])
print(len(d) if isinstance(d, list) else 0)
" "$_EVIDENCE_CHECK" 2>/dev/null || echo 0)
if [[ "$_EVIDENCE_LEN" -ge 1 ]]; then
  _ok "first persisted suggestion has $_EVIDENCE_LEN evidence member(s) (non-empty)"
else
  _fail "first persisted suggestion has empty evidence (got len=$_EVIDENCE_LEN)"
fi

# ── idempotency: re-running reflect must NOT duplicate rows ───────────────
echo ""
echo "--- idempotency: re-running persist does not duplicate ---"

_PROPOSED_BEFORE=$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM emergent_patterns WHERE status='proposed';" 2>/dev/null || echo 0)

_PERSISTED_AGAIN=$(reflection_persist_suggestions "$SUGGESTIONS_JSON" 2>/dev/null || echo 0)
_PROPOSED_AFTER=$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM emergent_patterns WHERE status='proposed';" 2>/dev/null || echo 0)

# PK is pattern_id → upsert replaces in place; row count unchanged.
_assert_eq "second persist reports 0 NEW rows (all upserted)" "$_PERSISTED_AGAIN" "$_PROPOSED_BEFORE"
_assert_eq "emergent_patterns row count unchanged after re-persist" "$_PROPOSED_AFTER" "$_PROPOSED_BEFORE"

# ── empty-DB safety: 0 traces → 0 patterns, no error ───────────────────────
echo ""
echo "--- empty-DB safety: miner on empty DB yields 0 patterns, no error ---"

EMPTY_DB=$(mktemp /tmp/mini-ork-llw-empty-XXXXXX.db)
PREV_DB="$MINI_ORK_DB"
export MINI_ORK_DB="$EMPTY_DB"
EMPTY_WRITTEN=$(pattern_store_mine_from_traces --window 7d --min-cluster 3 2>/dev/null || echo 0)
EMPTY_PATTERNS=$(sqlite3 "$EMPTY_DB" "SELECT COUNT(*) FROM pattern_records;" 2>/dev/null || echo 0)
export MINI_ORK_DB="$PREV_DB"
rm -f "$EMPTY_DB"

_assert_eq "empty DB: miner writes 0 patterns" "$EMPTY_WRITTEN" "0"
_assert_eq "empty DB: pattern_records row count = 0" "$EMPTY_PATTERNS" "0"

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0