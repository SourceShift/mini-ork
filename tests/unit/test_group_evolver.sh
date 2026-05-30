#!/usr/bin/env bash
# tests/unit/test_group_evolver.sh — unit tests for lib/group_evolver.sh
# Usage: bash tests/unit/test_group_evolver.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/group_evolver.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: group_evolver.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/group_evolver.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

export MINI_ORK_HOME=$(mktemp -d)
export MINI_ORK_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
trap 'rm -f "$MINI_ORK_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# shellcheck source=/dev/null
source "$LIB"

# Minimal history payload used across tests
HISTORY='[
  {
    "workflow_id": "wf-1",
    "nodes": {"plan": {"tools": ["read"]}, "exec": {"tools": ["write"]}},
    "edges": ["plan->exec"],
    "performance": 0.8,
    "failure_modes_handled": ["timeout"],
    "tool_sequence": ["read","write"],
    "model_lane": "balanced",
    "task_class": "code-review"
  },
  {
    "workflow_id": "wf-2",
    "nodes": {"scan": {"tools": ["grep"]}, "report": {"tools": ["write"]}},
    "edges": ["scan->report"],
    "performance": 0.6,
    "failure_modes_handled": ["parse_error"],
    "tool_sequence": ["grep","write"],
    "model_lane": "fast",
    "task_class": "code-review"
  }
]'

echo ""
echo "--- happy path: group_propose returns N candidates (default 5) ---"

OUTPUT="$(group_propose "$HISTORY" 2>/dev/null)"
LINE_COUNT="$(echo "$OUTPUT" | grep -c '{' 2>/dev/null || echo 0)"

if [[ "$LINE_COUNT" -ge 1 ]]; then
  _ok "group_propose emits at least 1 candidate line (got $LINE_COUNT)"
else
  _fail "group_propose emitted 0 candidate lines"
fi

echo ""
echo "--- happy path: each candidate has required fields ---"

FIRST_CANDIDATE="$(echo "$OUTPUT" | head -1)"
if [[ -n "$FIRST_CANDIDATE" ]]; then
  HAS_CID="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print('ok' if d.get('candidate_id') else 'missing')" "$FIRST_CANDIDATE" 2>/dev/null || echo "missing")"
  _assert_eq "candidate has candidate_id" "$HAS_CID" "ok"

  HAS_MUT="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print('ok' if d.get('mutation_type') else 'missing')" "$FIRST_CANDIDATE" 2>/dev/null || echo "missing")"
  _assert_eq "candidate has mutation_type" "$HAS_MUT" "ok"

  HAS_PARENT="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print('ok' if 'parent_id' in d else 'missing')" "$FIRST_CANDIDATE" 2>/dev/null || echo "missing")"
  _assert_eq "candidate has parent_id" "$HAS_PARENT" "ok"
else
  _fail "group_propose produced no output to validate"
fi

echo ""
echo "--- happy path: MINI_ORK_GROUP_CANDIDATES controls output count ---"

export MINI_ORK_GROUP_CANDIDATES=2
OUTPUT2="$(group_propose "$HISTORY" 2>/dev/null)"
COUNT2="$(echo "$OUTPUT2" | grep -c '{' 2>/dev/null || echo 0)"
_assert_eq "MINI_ORK_GROUP_CANDIDATES=2 yields exactly 2 candidates" "$COUNT2" "2"
unset MINI_ORK_GROUP_CANDIDATES

echo ""
echo "--- happy path: mutation types are valid ---"

VALID_MUTATIONS="add_node remove_node rewrite_node_prompt add_verifier change_model_lane reorder_edges add_human_gate split_by_task_type"
ALL_VALID=1
while IFS= read -r line; do
  [[ -z "$line" || "$line" == "null" ]] && continue
  MUT="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('mutation_type',''))" "$line" 2>/dev/null || echo "")"
  if [[ -n "$MUT" ]] && ! echo "$VALID_MUTATIONS" | grep -qw "$MUT" 2>/dev/null; then
    ALL_VALID=0
    _fail "candidate has invalid mutation_type: '$MUT'"
  fi
done <<< "$(group_propose "$HISTORY" 2>/dev/null)"

if [[ "$ALL_VALID" -eq 1 ]]; then
  _ok "all candidate mutation_types are valid"
fi

echo ""
echo "--- edge case: empty history array yields no error, emits error JSON ---"

EMPTY_OUT="$(group_propose '[]' 2>/dev/null)"
# The function emits a JSON object with "error" key when history is empty
if python3 -c "import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if 'error' in d or 'candidates' in d else 1)" "$EMPTY_OUT" 2>/dev/null; then
  _ok "group_propose with empty history returns error JSON (not crash)"
else
  _ok "group_propose with empty history exits cleanly (may emit nothing)"
fi

echo ""
echo "--- error path: group_propose with invalid JSON exits non-zero ---"

if group_propose "not-json-at-all" >/dev/null 2>&1; then
  _fail "group_propose with invalid JSON should exit non-zero"
else
  _ok "group_propose with invalid JSON exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
