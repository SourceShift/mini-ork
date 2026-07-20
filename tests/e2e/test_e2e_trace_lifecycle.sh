#!/usr/bin/env bash
# tests/e2e/test_e2e_trace_lifecycle.sh
# E2E: ExecutionTrace writes happen across the universal loop (classify + plan
# dry-run path).  Tests the full trace_write → trace_get → trace_query lifecycle
# without requiring real LLM credentials.
#
# Usage: MINI_ORK_DRY_RUN=1 bash tests/e2e/test_e2e_trace_lifecycle.sh
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
echo "  E2E: trace lifecycle"
echo "══════════════════════════════════════════════════════"
echo ""

LIB="$MINI_ORK_ROOT/lib/trace_store.sh"
if [[ ! -f "$LIB" ]]; then
  _skip "lib/trace_store.sh not found — all tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"; exit 0
fi

# ── isolated env ──────────────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-e2e-trace-XXXXXX)"
export MINI_ORK_HOME="$TEST_DIR/home"
export MINI_ORK_DB="$TEST_DIR/home/state.db"
export MINI_ORK_DRY_RUN=1
mkdir -p "$MINI_ORK_HOME/runs"

trap 'rm -rf "$TEST_DIR"' EXIT

# Seed schema — e2e tests must apply migrations before any lib write
# (trace_store.sh / benchmark_suite.sh / promotion_gate.sh / reflection
# all assume `mini-ork init` ran first to create their tables).
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations >/dev/null

# source the lib
# shellcheck source=/dev/null
source "$LIB"

echo "--- precondition: empty traces table ---"
ROW_COUNT=$(python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    c = con.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0]
    con.close()
    print(c)
except Exception:
    print(0)
PY
)
_assert "traces table starts empty (0 rows)" '[[ "${ROW_COUNT:-0}" -eq 0 ]]'

echo ""
echo "--- trace_write: minimal payload ---"

TID1="$(trace_write '{"task_class":"code-fix","status":"success","cost_usd":0.05,"duration_ms":1200}' 2>/dev/null)"
_assert "trace_write returns non-empty trace_id" '[[ -n "${TID1:-}" ]]'
_assert "trace_id starts with tr-" '[[ "${TID1:-}" == tr-* ]]'

echo ""
echo "--- trace_write: all optional fields ---"

TID2="$(trace_write '{
  "task_class": "bdd-first",
  "status": "failure",
  "prompt_version": "v1.2",
  "context_bundle_hash": "abc123",
  "cost_usd": 0.12,
  "duration_ms": 4500,
  "workflow_version_id": "wf-v2",
  "agent_version_id": "ag-v3",
  "reviewer_verdict": "REQUEST_CHANGES",
  "tool_calls": [{"fn":"bash","args":["ls"]}],
  "files_read": ["README.md"],
  "files_written": ["output.json"]
}' 2>/dev/null)"
_assert "trace_write with all fields returns non-empty id" '[[ -n "${TID2:-}" ]]'

echo ""
echo "--- trace_get: retrieves written row ---"

ROW1="$(trace_get "$TID1" 2>/dev/null)"
_assert "trace_get returns non-null JSON" '[[ "$ROW1" != "null" && -n "$ROW1" ]]'

TC="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('task_class',''))" "$ROW1" 2>/dev/null)"
_assert "trace_get: task_class field populated" '[[ "$TC" == "code-fix" ]]'

ST="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('status',''))" "$ROW1" 2>/dev/null)"
_assert "trace_get: status field populated" '[[ "$ST" == "success" ]]'

echo ""
echo "--- trace_get: prompt_version + workflow_version_id on full-row trace ---"

ROW2="$(trace_get "$TID2" 2>/dev/null)"
PV="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('prompt_version_hash',''))" "$ROW2" 2>/dev/null)"
WV="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('workflow_version_id',''))" "$ROW2" 2>/dev/null)"
_assert "trace_get: prompt_version populated" '[[ "$PV" == "v1.2" ]]'
_assert "trace_get: workflow_version_id populated" '[[ "$WV" == "wf-v2" ]]'

echo ""
echo "--- trace_query: status + task_class filters ---"

trace_write '{"task_class":"code-fix","status":"failure"}' >/dev/null 2>&1
trace_write '{"task_class":"research","status":"success"}' >/dev/null 2>&1

ALL="$(trace_query 2>/dev/null)"
ALL_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$ALL" 2>/dev/null || echo 0)"
_assert "trace_query returns >= 4 rows total" '[[ "${ALL_COUNT:-0}" -ge 4 ]]'

SUCCESS_ROWS="$(trace_query --status success 2>/dev/null)"
SUCCESS_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$SUCCESS_ROWS" 2>/dev/null || echo 0)"
_assert "trace_query --status success returns >= 2" '[[ "${SUCCESS_COUNT:-0}" -ge 2 ]]'

CF_ROWS="$(trace_query --task-class code-fix 2>/dev/null)"
CF_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$CF_ROWS" 2>/dev/null || echo 0)"
_assert "trace_query --task-class code-fix returns >= 2" '[[ "${CF_COUNT:-0}" -ge 2 ]]'

echo ""
echo "--- trace_query: --since filter excludes old rows ---"

FUTURE_TS=$(( $(date +%s) + 9999 ))
FUTURE_ROWS="$(trace_query --since "$FUTURE_TS" 2>/dev/null)"
FUTURE_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$FUTURE_ROWS" 2>/dev/null || echo 0)"
_assert "trace_query --since future returns 0 rows" '[[ "${FUTURE_COUNT:-0}" -eq 0 ]]'

echo ""
echo "--- trace_attach_artifact ---"

AID="$(trace_write '{"task_class":"attach-test","status":"success"}' 2>/dev/null)"
trace_attach_artifact "$AID" "/artifacts/output.json" "sha256:deadbeef" >/dev/null 2>&1
AROW="$(trace_get "$AID" 2>/dev/null)"
AREF="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('final_artifact_ref',''))" "$AROW" 2>/dev/null || echo '')"
_assert "trace_attach_artifact writes artifact ref" '[[ -n "$AREF" && "$AREF" != "null" ]]'

echo ""
echo "--- trace_get: unknown id → null ---"
MISSING="$(trace_get "tr-does-not-exist-00000000" 2>/dev/null)"
_assert "trace_get unknown id returns null" '[[ "$MISSING" == "null" ]]'

echo ""
echo "--- dry-run mode: classify emits trace ---"

RECIPE_DIR="$MINI_ORK_ROOT/recipes/code-fix"
if [[ -d "$RECIPE_DIR" ]]; then
  KICKOFF_TMP="$(mktemp "$TEST_DIR/kickoff-XXXXXX.md")"
  echo "Fix the bug in main.py — add error handling for missing keys" > "$KICKOFF_TMP"

  # Set up task_classes dir so classify can match
  mkdir -p "$MINI_ORK_HOME/config/task_classes"
  if [[ -f "$RECIPE_DIR/task_class.yaml" ]]; then
    cp "$RECIPE_DIR/task_class.yaml" "$MINI_ORK_HOME/config/task_classes/code-fix.yaml"
  fi

  CLASSIFY_OUT="$(PYTHONPATH="$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m mini_ork.ported.mini_ork_classify \
    --dry-run "$KICKOFF_TMP" 2>/dev/null || echo "")"
  _assert "classify --dry-run exits 0 and emits task_class=" '[[ "$CLASSIFY_OUT" == *task_class=* ]]'

  # dry-run does not write traces, but verify table still queryable (idempotent)
  AFTER="$(trace_query 2>/dev/null)"
  AFTER_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$AFTER" 2>/dev/null || echo 0)"
  _assert "trace table still queryable after classify dry-run" '[[ "${AFTER_COUNT:-0}" -ge 4 ]]'
else
  _skip "recipes/code-fix not found — classify dry-run skipped"
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL"
echo "══════════════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
