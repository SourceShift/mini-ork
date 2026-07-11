#!/usr/bin/env bash
# tests/unit/test_gradient_dedup_semantic.sh — semantic-signature dedup tests.
#
# Covers AC3 + AC4 of the Gr1/Gr2 fixes (kickoff: GEPA gradient fixes):
#   AC3: three reviewer rows that differ only in per-trace noise tokens
#        (durations, costs, trace ids) collapse to ONE surviving row.
#   AC4: two rows with the same target but materially different
#        suggested_change intent must NOT merge.
#
# Run standalone:
#   bash tests/unit/test_gradient_dedup_semantic.sh
# Exit 0 = all assertions pass; non-zero on any FAIL.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/reflection_pipeline.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: reflection_deduplicate (semantic signature) ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/reflection_pipeline.sh not found"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TEST_DB=$(mktemp /tmp/mini-ork-semantic-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Migration scaffold
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations || { echo "skip: migrations failed"; exit 0; }

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/trace_store.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/gradient_extractor.sh"
# shellcheck source=/dev/null
source "$LIB"

# ── AC3: same-target trace-token collapse ─────────────────────────────────
echo ""
echo "--- AC3: 3 reviewer rows differing only in trace-token noise collapse to 1 ---"

python3 - <<PY
import sqlite3, time
con = sqlite3.connect("$TEST_DB")
con.execute("""
    CREATE TABLE IF NOT EXISTS gradient_records (
        gradient_id   TEXT PRIMARY KEY,
        target        TEXT NOT NULL,
        signal        TEXT NOT NULL,
        suggested_change TEXT NOT NULL,
        evidence      TEXT NOT NULL,
        confidence    REAL NOT NULL DEFAULT 0.0,
        created_at    INTEGER NOT NULL,
        task_class    TEXT
    )
""")
now = int(time.time())
rows = [
    ("gr-trace-1",
     "agent.reviewer.prompt",
     "verifier spent 2.7min and cost \$1.62 on the empty-object fix",
     "add a guard against empty verifier_output before re-extracting",
     "tr-aaaa1111", 0.55),
    ("gr-trace-2",
     "agent.reviewer.prompt",
     "verifier spent 8.9min and cost \$5.10 on the empty-object fix",
     "add a guard against empty verifier_output before re-extracting",
     "tr-bbbb2222", 0.62),
    ("gr-trace-3",
     "agent.reviewer.prompt",
     "verifier spent 633s and cost \$3.53 on the empty-object fix",
     "add a guard against empty verifier_output before re-extracting",
     "tr-cccc3333", 0.50),
]
for gid, tgt, sig, chg, ev, conf in rows:
    con.execute(
        "INSERT INTO gradient_records"
        " (gradient_id, target, signal, suggested_change, evidence,"
        "  confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (gid, tgt, sig, chg, ev, conf, now, "framework_edit"),
    )
con.commit()
con.close()
PY

BEFORE="$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM gradient_records WHERE target='agent.reviewer.prompt';" 2>/dev/null || echo 0)"
reflection_deduplicate "gradient_records" >/dev/null 2>&1 || true
AFTER="$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM gradient_records WHERE target='agent.reviewer.prompt';" 2>/dev/null || echo 0)"

if [[ "$BEFORE" -eq 3 && "$AFTER" -eq 1 ]]; then
  _ok "AC3: collapsed 3 trace-noisy reviewer rows to 1 (before=$BEFORE after=$AFTER)"
else
  _fail "AC3: row collapse wrong (before=$BEFORE after=$AFTER — expected 3→1)"
fi

# Sanity: surviving row keeps the highest-confidence version (legacy order)
KEPT="$(sqlite3 "$TEST_DB" \
  "SELECT gradient_id FROM gradient_records WHERE target='agent.reviewer.prompt' LIMIT 1;" 2>/dev/null || echo "")"
if [[ "$KEPT" == "gr-trace-2" ]]; then
  _ok "AC3: kept highest-confidence row (gr-trace-2 @ 0.62)"
else
  _fail "AC3: kept wrong row id '$KEPT' (expected gr-trace-2)"
fi

# ── AC4: distinct intents MUST NOT merge ─────────────────────────────────
echo ""
echo "--- AC4: 2 rows with distinct suggested_change intent survive ---"

python3 - <<PY
import sqlite3, time
con = sqlite3.connect("$TEST_DB")
now = int(time.time())
rows = [
    ("gr-intent-A",
     "agent.planner.prompt",
     "planner skipped the verifier link step in the original trace",
     "inject the verifier_output schema before the planner prompt",
     "tr-1111aaaa", 0.7),
    ("gr-intent-B",
     "agent.planner.prompt",
     "planner emitted the wrong aggregation for the panel review",
     "switch the lens-count aggregator from sum to majority_vote",
     "tr-2222bbbb", 0.8),
]
for gid, tgt, sig, chg, ev, conf in rows:
    con.execute(
        "INSERT INTO gradient_records"
        " (gradient_id, target, signal, suggested_change, evidence,"
        "  confidence, created_at, task_class)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (gid, tgt, sig, chg, ev, conf, now, "framework_edit"),
    )
# Clear AC3 residue first so it does not pollute — we want a clean assertion.
con.execute(
    "DELETE FROM gradient_records WHERE target='agent.reviewer.prompt'",
)
con.commit()
con.close()
PY

BEFORE_A="$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM gradient_records;" 2>/dev/null || echo 0)"
reflection_deduplicate "gradient_records" >/dev/null 2>&1 || true
AFTER_A="$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM gradient_records;" 2>/dev/null || echo 0)"

if [[ "$BEFORE_A" -eq 2 && "$AFTER_A" -eq 2 ]]; then
  _ok "AC4: 2 distinct-intent rows survive dedup (count $BEFORE_A → $AFTER_A)"
else
  _fail "AC4: distinct-intent rows merged (before=$BEFORE_A after=$AFTER_A — expected 2→2)"
fi

# Confirm exact rows still present
ROW_A="$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM gradient_records WHERE gradient_id='gr-intent-A';" 2>/dev/null || echo 0)"
ROW_B="$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM gradient_records WHERE gradient_id='gr-intent-B';" 2>/dev/null || echo 0)"
if [[ "$ROW_A" -eq 1 && "$ROW_B" -eq 1 ]]; then
  _ok "AC4: both intent-A and intent-B rows preserved"
else
  _fail "AC4: lost a distinct-intent row (A=$ROW_A B=$ROW_B)"
fi

# ── Regression: existing happy path still passes ─────────────────────────
echo ""
echo "--- regression: existing exact (target,signal) dedup still works ---"

python3 - <<PY
import sqlite3, time
con = sqlite3.connect("$TEST_DB")
now = int(time.time())
con.execute("""
    CREATE TABLE IF NOT EXISTS gradient_records (
        gradient_id TEXT PRIMARY KEY, target TEXT NOT NULL, signal TEXT NOT NULL,
        suggested_change TEXT NOT NULL, evidence TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.0, created_at INTEGER NOT NULL,
        task_class TEXT)
""")
con.execute("INSERT OR IGNORE INTO gradient_records VALUES (?,?,?,?,?,?,?,?)",
            ("gr-legacy-1","wf.node.A","sig1","chg1","tr-1",0.3,now,""))
con.execute("INSERT OR IGNORE INTO gradient_records VALUES (?,?,?,?,?,?,?,?)",
            ("gr-legacy-2","wf.node.A","sig1","chg2","tr-2",0.8,now,""))
con.commit()
con.close()
PY

BEFORE_L="$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM gradient_records WHERE target='wf.node.A' AND signal='sig1';" 2>/dev/null || echo 0)"
reflection_deduplicate "gradient_records" >/dev/null 2>&1 || true
AFTER_L="$(sqlite3 "$TEST_DB" \
  "SELECT COUNT(*) FROM gradient_records WHERE target='wf.node.A' AND signal='sig1';" 2>/dev/null || echo 0)"

if [[ "$BEFORE_L" -eq 2 && "$AFTER_L" -eq 1 ]]; then
  _ok "legacy exact-target dedup unchanged (2 → 1)"
else
  _fail "legacy exact dedup regressed (before=$BEFORE_L after=$AFTER_L)"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
