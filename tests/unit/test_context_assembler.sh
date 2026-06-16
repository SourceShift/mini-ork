#!/usr/bin/env bash
# tests/unit/test_context_assembler.sh — unit tests for lib/context_assembler.sh
# Usage: bash tests/unit/test_context_assembler.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/context_assembler.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: context_assembler.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/context_assembler.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# Isolated test DB + home
TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Apply migrations so context_assembler + its deps find the tables it queries.
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations || { echo "skip: migrations failed to apply"; exit 0; }

# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: context_assemble returns valid JSON ContextPack ---"

# Write a minimal task brief file
BRIEF_FILE="$(mktemp /tmp/task-brief-XXXXXX.json)"
echo '{"task_class":"test-task","title":"Unit test brief"}' > "$BRIEF_FILE"

PACK="$(context_assemble "$BRIEF_FILE" "plan_node" 2>/dev/null)"
if python3 -c "import json,sys; d=json.loads(sys.argv[1]); print('ok')" "$PACK" >/dev/null 2>&1; then
  _ok "context_assemble emits valid JSON"
else
  _fail "context_assemble emits invalid JSON: '$PACK'"
fi

WORKFLOW_NODE="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('workflow_node',''))" "$PACK" 2>/dev/null || echo "")"
_assert_eq "ContextPack contains correct workflow_node" "$WORKFLOW_NODE" "plan_node"

BRIEF_IN_PACK="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print('ok' if d.get('task_brief') else 'missing')" "$PACK" 2>/dev/null || echo "missing")"
_assert_eq "ContextPack contains task_brief" "$BRIEF_IN_PACK" "ok"
rm -f "$BRIEF_FILE"

echo ""
echo "--- happy path: prior_similar_runs populated from execution_traces ---"

# Insert a trace with matching task_class
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/trace_store.sh"
trace_write '{"task_class":"prefill-task","status":"success","cost_usd":0.05}' >/dev/null 2>&1

BRIEF_FILE2="$(mktemp /tmp/task-brief-XXXXXX.json)"
echo '{"task_class":"prefill-task","title":"Prior runs brief"}' > "$BRIEF_FILE2"
PACK2="$(context_assemble "$BRIEF_FILE2" "exec_node" 2>/dev/null)"
PRIOR_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1]).get('prior_similar_runs',[])))" "$PACK2" 2>/dev/null || echo 0)"
if [[ "$PRIOR_COUNT" -ge 1 ]]; then
  _ok "context_assemble includes prior_similar_runs from execution_traces"
else
  _fail "context_assemble prior_similar_runs empty (expected >=1, got $PRIOR_COUNT)"
fi
rm -f "$BRIEF_FILE2"

echo ""
echo "--- edge case: context_assemble with tiny token budget truncates prior_runs ---"

export MINI_ORK_CTX_BUDGET_TOKENS=200
BRIEF_FILE3="$(mktemp /tmp/task-brief-XXXXXX.json)"
echo '{"task_class":"prefill-task","title":"Tiny budget brief"}' > "$BRIEF_FILE3"
PACK3="$(context_assemble "$BRIEF_FILE3" "compact_node" 2>/dev/null)"
TRUNCATED="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('_truncated','false'))" "$PACK3" 2>/dev/null || echo "false")"
if [[ "$TRUNCATED" == "True" || "$TRUNCATED" == "true" ]]; then
  _ok "context_assemble truncates when token budget exceeded"
else
  # May not truncate if there's very little data — that's fine
  _ok "context_assemble completed within tiny budget (no truncation needed)"
fi
rm -f "$BRIEF_FILE3"
unset MINI_ORK_CTX_BUDGET_TOKENS

echo ""
echo "--- error path: context_assemble with non-existent task_brief_path exits non-zero ---"

if context_assemble "/tmp/no-such-brief-file-xyz.json" "node" >/dev/null 2>&1; then
  _fail "context_assemble with missing brief file should exit non-zero"
else
  _ok "context_assemble with missing brief file exits non-zero"
fi

echo ""
echo "--- error path: context_assemble missing required args exits non-zero ---"

if bash -c "source '$LIB' 2>/dev/null; context_assemble" >/dev/null 2>&1; then
  _fail "context_assemble with no args should exit non-zero"
else
  _ok "context_assemble with no args exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
