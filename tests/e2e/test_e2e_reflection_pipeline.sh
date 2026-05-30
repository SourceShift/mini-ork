#!/usr/bin/env bash
# tests/e2e/test_e2e_reflection_pipeline.sh
# E2E: gradient extraction + pattern emergence from seeded execution traces.
# All LLM calls replaced by MINI_ORK_GRADIENT_EXTRACTOR_FN stub that returns
# deterministic gradients keyed on trace status/task_class.
#
# Usage: bash tests/e2e/test_e2e_reflection_pipeline.sh
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
echo "  E2E: reflection pipeline"
echo "══════════════════════════════════════════════════════"
echo ""

for lib in trace_store gradient_extractor pattern_store reflection_pipeline; do
  if [[ ! -f "$MINI_ORK_ROOT/lib/${lib}.sh" ]]; then
    _skip "lib/${lib}.sh not found — all tests deferred"
    echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"; exit 0
  fi
done

# ── isolated env ──────────────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-e2e-reflect-XXXXXX)"
export MINI_ORK_HOME="$TEST_DIR/home"
export MINI_ORK_DB="$TEST_DIR/home/state.db"
mkdir -p "$MINI_ORK_HOME"
trap 'rm -rf "$TEST_DIR"' EXIT

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/trace_store.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/gradient_extractor.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/pattern_store.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/reflection_pipeline.sh"

# ── LLM stub: deterministic gradient extractor ────────────────────────────────
# Returns 1 gradient per trace, with signal keyed on the trace's task_class+status.
# Two distinct failure patterns: "missing_context" and "timeout_error".
_stub_gradient_extractor() {
  local tid="$1"
  local trace_json="$2"
  local tc status signal
  tc="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('task_class','unknown'))" "$trace_json" 2>/dev/null || echo 'unknown')"
  status="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('status','unknown'))" "$trace_json" 2>/dev/null || echo 'unknown')"

  if [[ "$status" == "failure" && "$tc" == "pattern-a"* ]]; then
    signal="missing_context"
    echo "{\"target\":\"workflow.node.executor\",\"signal\":\"${signal}\",\"suggested_change\":\"add context window pre-fetch\",\"evidence\":\"${tid}\",\"confidence\":0.85}"
  elif [[ "$status" == "failure" && "$tc" == "pattern-b"* ]]; then
    signal="timeout_error"
    echo "{\"target\":\"workflow.node.verifier\",\"signal\":\"${signal}\",\"suggested_change\":\"increase timeout to 300s\",\"evidence\":\"${tid}\",\"confidence\":0.70}"
  else
    # success traces → no gradient
    true
  fi
}
export -f _stub_gradient_extractor
export MINI_ORK_GRADIENT_EXTRACTOR_FN="_stub_gradient_extractor"

echo "--- seed: 5 synthetic execution traces (2 distinct failure patterns) ---"

# 3 traces for pattern A (missing_context failures)
TID_A1="$(trace_write '{"task_class":"pattern-a-run1","status":"failure","cost_usd":0.01}' 2>/dev/null)"
TID_A2="$(trace_write '{"task_class":"pattern-a-run2","status":"failure","cost_usd":0.01}' 2>/dev/null)"
TID_A3="$(trace_write '{"task_class":"pattern-a-run3","status":"failure","cost_usd":0.01}' 2>/dev/null)"
# 2 traces for pattern B (timeout_error failures)
TID_B1="$(trace_write '{"task_class":"pattern-b-run1","status":"failure","cost_usd":0.02}' 2>/dev/null)"
TID_B2="$(trace_write '{"task_class":"pattern-b-run2","status":"failure","cost_usd":0.02}' 2>/dev/null)"

_assert "seeded 5 traces (TID_A1 non-empty)" '[[ -n "${TID_A1:-}" ]]'
_assert "seeded 5 traces (TID_B2 non-empty)" '[[ -n "${TID_B2:-}" ]]'

echo ""
echo "--- gradient extraction per trace ---"

for tid in "$TID_A1" "$TID_A2" "$TID_A3" "$TID_B1" "$TID_B2"; do
  G="$(gradient_extract "$tid" 2>/dev/null || true)"
  if [[ -n "$G" ]]; then
    gradient_store "$G" >/dev/null 2>&1 || true
  fi
done

GRAD_COUNT="$(python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    c = con.execute("SELECT COUNT(*) FROM gradient_records").fetchone()[0]
    con.close()
    print(c)
except Exception:
    print(0)
PY
)"
_assert "gradient_records has >= 5 rows after extraction" '[[ "${GRAD_COUNT:-0}" -ge 5 ]]'

echo ""
echo "--- gradient_store: each gradient links to source trace via evidence field ---"

EV_CHECK="$(python3 - "$MINI_ORK_DB" "$TID_A1" <<'PY'
import sqlite3, sys
db, tid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
row = con.execute("SELECT COUNT(*) FROM gradient_records WHERE evidence=?", (tid,)).fetchone()
con.close()
print(row[0] if row else 0)
PY
)"
_assert "gradient links back to evidence trace_id (FK)" '[[ "${EV_CHECK:-0}" -ge 1 ]]'

echo ""
echo "--- pattern_store: store recurring patterns + detect frequency ---"

# Store 3 pattern-A records with the same pattern_id to bump frequency
PAT_A_ID="pat-missing-context-001"
pattern_store "{\"pattern_id\":\"${PAT_A_ID}\",\"description\":\"missing_context failure recurring\",\"evidence_trace_ids\":[\"${TID_A1}\"],\"output_type\":\"workflow_change\",\"frequency\":1}" >/dev/null 2>&1
pattern_store "{\"pattern_id\":\"${PAT_A_ID}\",\"description\":\"missing_context failure recurring\",\"evidence_trace_ids\":[\"${TID_A2}\"],\"output_type\":\"workflow_change\"}" >/dev/null 2>&1
pattern_store "{\"pattern_id\":\"${PAT_A_ID}\",\"description\":\"missing_context failure recurring\",\"evidence_trace_ids\":[\"${TID_A3}\"],\"output_type\":\"workflow_change\"}" >/dev/null 2>&1

PAT_B_ID="pat-timeout-error-001"
pattern_store "{\"pattern_id\":\"${PAT_B_ID}\",\"description\":\"timeout_error failure recurring\",\"evidence_trace_ids\":[\"${TID_B1}\"],\"output_type\":\"workflow_change\"}" >/dev/null 2>&1
pattern_store "{\"pattern_id\":\"${PAT_B_ID}\",\"description\":\"timeout_error failure recurring\",\"evidence_trace_ids\":[\"${TID_B2}\"],\"output_type\":\"workflow_change\"}" >/dev/null 2>&1

echo ""
echo "--- pattern_query: pattern A frequency should be >= 3 ---"

PAT_A_ROW="$(python3 - "$MINI_ORK_DB" "$PAT_A_ID" <<'PY'
import sqlite3, json, sys
db, pid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute("SELECT * FROM pattern_records WHERE pattern_id=?", (pid,)).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
)"
PAT_A_FREQ="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('frequency',0))" "$PAT_A_ROW" 2>/dev/null || echo 0)"
_assert "pattern A frequency >= 3 (upsert bumped each time)" '[[ "${PAT_A_FREQ:-0}" -ge 3 ]]'

echo ""
echo "--- pattern_query --min-frequency 2 returns both patterns ---"

HIGH_FREQ="$(pattern_query --min-frequency 2 2>/dev/null)"
HIGH_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$HIGH_FREQ" 2>/dev/null || echo 0)"
_assert "pattern_query --min-frequency 2 returns >= 2 patterns" '[[ "${HIGH_COUNT:-0}" -ge 2 ]]'

echo ""
echo "--- reflection_pipeline: evidence_trace_ids merged across upserts ---"

MERGED="$(python3 - "$MINI_ORK_DB" "$PAT_A_ID" <<'PY'
import sqlite3, json, sys
db, pid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
row = con.execute("SELECT evidence_trace_ids FROM pattern_records WHERE pattern_id=?", (pid,)).fetchone()
con.close()
if row:
    ids = json.loads(row[0]) if row[0] else []
    print(len(ids))
else:
    print(0)
PY
)"
_assert "pattern A evidence_trace_ids has >= 2 distinct entries after merges" '[[ "${MERGED:-0}" -ge 2 ]]'

echo ""
echo "--- reflection_run: orchestrates all steps without LLM crash ---"

SINCE_TS=0
ROUT="$(reflection_run --since "$SINCE_TS" 2>/dev/null || echo "FAILED")"
_assert "reflection_run completes without crashing" '[[ "$ROUT" != "FAILED" ]]'

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
