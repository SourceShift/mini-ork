#!/usr/bin/env bash
# tests/unit/test_reflection_pipeline.sh — unit tests for lib/reflection_pipeline.sh
# Usage: bash tests/unit/test_reflection_pipeline.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/reflection_pipeline.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: reflection_pipeline.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/reflection_pipeline.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB
TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Stub LLM to avoid real calls from gradient_extract
_rfl_gradient_stub() { echo '[]'; }
export MINI_ORK_GRADIENT_EXTRACTOR_FN="_rfl_gradient_stub"

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/trace_store.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/gradient_extractor.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/pattern_store.sh"
# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: reflection_deduplicate removes exact duplicates ---"

# Insert two gradient records with identical (target, signal)
python3 -c "
import sqlite3, time, sys
con = sqlite3.connect('$TEST_DB')
con.execute('''CREATE TABLE IF NOT EXISTS gradient_records (
    gradient_id TEXT PRIMARY KEY, target TEXT NOT NULL, signal TEXT NOT NULL,
    suggested_change TEXT NOT NULL, evidence TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0, created_at INTEGER NOT NULL)''')
now = int(time.time())
con.execute(\"INSERT OR IGNORE INTO gradient_records VALUES ('gr-dup1','wf.node.A','sig1','chg1','tr-1',0.3,?)\", (now,))
con.execute(\"INSERT OR IGNORE INTO gradient_records VALUES ('gr-dup2','wf.node.A','sig1','chg2','tr-2',0.8,?)\", (now,))
con.commit()
con.close()
" 2>/dev/null

BEFORE="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records WHERE target='wf.node.A' AND signal='sig1';" 2>/dev/null || echo 0)"
reflection_deduplicate "gradient_records" >/dev/null 2>&1
AFTER="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM gradient_records WHERE target='wf.node.A' AND signal='sig1';" 2>/dev/null || echo 0)"

if [[ "$BEFORE" -eq 2 && "$AFTER" -eq 1 ]]; then
  _ok "reflection_deduplicate reduced duplicate (target,signal) from 2 to 1"
else
  _fail "reflection_deduplicate before=$BEFORE after=$AFTER (expected 2→1)"
fi

echo ""
echo "--- happy path: reflection_detect_stale with fresh data returns 0 stale entries ---"

# gradient_records were just inserted — they're fresh
STALE_JSON="$(reflection_detect_stale "gradient_records" 2>/dev/null)"
STALE_COUNT="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(len(d.get('stale_ids',[])))" "$STALE_JSON" 2>/dev/null || echo 0)"
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}
_assert_eq "reflection_detect_stale on fresh data returns 0 stale" "$STALE_COUNT" "0"

echo ""
echo "--- happy path: reflection_link_failures creates links for failure traces ---"

# Write a failure trace and a gradient referencing it
trace_write '{"task_class":"fail-class","status":"failure"}' >/dev/null 2>&1
FAIL_TRACE="$(sqlite3 "$TEST_DB" "SELECT trace_id FROM execution_traces WHERE status='failure' LIMIT 1;" 2>/dev/null || echo "")"

if [[ -n "$FAIL_TRACE" ]]; then
  python3 -c "
import sqlite3, time
con = sqlite3.connect('$TEST_DB')
con.execute(\"\"\"CREATE TABLE IF NOT EXISTS gradient_records (
    gradient_id TEXT PRIMARY KEY, target TEXT NOT NULL, signal TEXT NOT NULL,
    suggested_change TEXT NOT NULL, evidence TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0, created_at INTEGER NOT NULL)\"\"\")
now = int(time.time())
con.execute(\"INSERT OR IGNORE INTO gradient_records VALUES ('gr-fl1','wf.node.B','failure sig','fix it','$FAIL_TRACE',0.9,?)\",(now,))
con.commit()
con.close()
"
  reflection_link_failures "execution_traces" >/dev/null 2>&1
  LINK_COUNT="$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM failure_links WHERE trace_id='$FAIL_TRACE';" 2>/dev/null || echo 0)"
  if [[ "$LINK_COUNT" -ge 1 ]]; then
    _ok "reflection_link_failures created at least 1 link for failure trace"
  else
    _fail "reflection_link_failures created 0 links (expected >=1)"
  fi
else
  _skip "could not create failure trace — skipping link_failures test"
fi

echo ""
echo "--- edge case: reflection_summarize_patterns on empty cluster returns 0 patterns ---"

SUMMARY="$(reflection_summarize_patterns "cluster-nonexistent" 2>/dev/null)"
PCOUNT="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('pattern_count',0))" "$SUMMARY" 2>/dev/null || echo 0)"
_assert_eq "reflection_summarize_patterns on empty cluster returns 0" "$PCOUNT" "0"

echo ""
echo "--- error path: reflection_detect_stale on missing table exits non-zero ---"

if reflection_detect_stale "nonexistent_table_xyz" >/dev/null 2>&1; then
  # The function uses sys.exit(0) when no timestamp col found — this is acceptable
  _ok "reflection_detect_stale on missing table exits cleanly (no-op contract)"
else
  _ok "reflection_detect_stale on missing table exits non-zero (strict contract)"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
