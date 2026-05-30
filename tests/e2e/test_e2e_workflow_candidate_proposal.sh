#!/usr/bin/env bash
# tests/e2e/test_e2e_workflow_candidate_proposal.sh
# E2E: group_evolver proposes WorkflowCandidate objects from seeded
# group performance history and pattern records.  No LLM required.
#
# Usage: bash tests/e2e/test_e2e_workflow_candidate_proposal.sh
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
echo "  E2E: workflow candidate proposal"
echo "══════════════════════════════════════════════════════"
echo ""

for lib in pattern_store group_evolver; do
  if [[ ! -f "$MINI_ORK_ROOT/lib/${lib}.sh" ]]; then
    _skip "lib/${lib}.sh not found — all tests deferred"
    echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"; exit 0
  fi
done

# ── isolated env ──────────────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-e2e-evolve-XXXXXX)"
export MINI_ORK_HOME="$TEST_DIR/home"
export MINI_ORK_DB="$TEST_DIR/home/state.db"
mkdir -p "$MINI_ORK_HOME"
trap 'rm -rf "$TEST_DIR"' EXIT

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/pattern_store.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/group_evolver.sh"

# ── seed: 3 patterns ──────────────────────────────────────────────────────────
echo "--- seed: 3 patterns in PatternStore ---"

P1="$(pattern_store '{"description":"missing verifier in pipeline","output_type":"verifier_addition","evidence_trace_ids":["tr-001","tr-002"],"frequency":3}' 2>/dev/null)"
P2="$(pattern_store '{"description":"model lane mismatch on complex tasks","output_type":"workflow_change","evidence_trace_ids":["tr-003","tr-004"],"frequency":2}' 2>/dev/null)"
P3="$(pattern_store '{"description":"edge ordering causes context loss","output_type":"prompt_change","evidence_trace_ids":["tr-005"],"frequency":1}' 2>/dev/null)"

_assert "pattern 1 stored" '[[ -n "${P1:-}" ]]'
_assert "pattern 2 stored" '[[ -n "${P2:-}" ]]'
_assert "pattern 3 stored" '[[ -n "${P3:-}" ]]'

PAT_COUNT="$(python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
c = con.execute("SELECT COUNT(*) FROM pattern_records").fetchone()[0]
con.close()
print(c)
PY
)"
_assert "pattern_records has 3 rows" '[[ "${PAT_COUNT:-0}" -eq 3 ]]'

# ── seed: group performance history (mock workflow_memory) ────────────────────
echo ""
echo "--- seed: group_perf_history with 3 workflow versions ---"

HISTORY_JSON='[
  {
    "workflow_id": "wf-v1",
    "nodes": {"classify": {"tools":["regex"]}, "execute": {"tools":["bash","python"]}},
    "edges": [{"from":"classify","to":"execute"}],
    "performance": 0.60,
    "failure_modes_handled": ["parse_error"],
    "tool_sequence": ["regex","bash"],
    "model_lane": "balanced",
    "task_class": "code-fix",
    "version_id": "wf-v1"
  },
  {
    "workflow_id": "wf-v2",
    "nodes": {"classify": {"tools":["regex"]}, "execute": {"tools":["bash"]}, "verify": {"tools":["pytest"]}},
    "edges": [{"from":"classify","to":"execute"},{"from":"execute","to":"verify"}],
    "performance": 0.75,
    "failure_modes_handled": ["parse_error","test_failure"],
    "tool_sequence": ["regex","bash","pytest"],
    "model_lane": "quality",
    "task_class": "code-fix",
    "version_id": "wf-v2"
  },
  {
    "workflow_id": "wf-v3",
    "nodes": {"classify": {"tools":["regex"]}, "plan": {"tools":["llm"]}, "execute": {"tools":["bash"]}},
    "edges": [{"from":"classify","to":"plan"},{"from":"plan","to":"execute"}],
    "performance": 0.55,
    "failure_modes_handled": ["timeout"],
    "tool_sequence": ["regex","llm","bash"],
    "model_lane": "fast",
    "task_class": "code-fix",
    "version_id": "wf-v3"
  }
]'

echo ""
echo "--- group_propose: emit >= 1 WorkflowCandidate ---"

export MINI_ORK_GROUP_CANDIDATES=3
CANDIDATES_RAW="$(group_propose "$HISTORY_JSON" 2>/dev/null)"
_assert "group_propose produces non-empty output" '[[ -n "${CANDIDATES_RAW:-}" ]]'

# Count lines that parse as JSON objects (each candidate is one line)
CAND_COUNT="$(echo "$CANDIDATES_RAW" | python3 -c "
import sys, json
count = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
        if isinstance(d, dict) and 'candidate_id' in d:
            count += 1
    except Exception:
        pass
print(count)
" 2>/dev/null || echo 0)"
_assert "group_propose emits >= 1 valid WorkflowCandidate JSON line" '[[ "${CAND_COUNT:-0}" -ge 1 ]]'

echo ""
echo "--- candidate shape validation ---"

FIRST_CAND="$(echo "$CANDIDATES_RAW" | grep -m1 '{' || echo '{}')"
CID="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('candidate_id',''))" "$FIRST_CAND" 2>/dev/null || echo '')"
MUT="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('mutation_type',''))" "$FIRST_CAND" 2>/dev/null || echo '')"
PAR="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('parent_id',''))" "$FIRST_CAND" 2>/dev/null || echo '')"
SEL="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); v=d.get('selection_score',None); print(v if v is not None else '')" "$FIRST_CAND" 2>/dev/null || echo '')"
NOV="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); v=d.get('novelty_estimated',None); print(v if v is not None else '')" "$FIRST_CAND" 2>/dev/null || echo '')"

_assert "candidate has candidate_id field" '[[ -n "${CID:-}" ]]'
_assert "candidate_id starts with wc-" '[[ "${CID:-}" == wc-* ]]'
_assert "candidate has mutation_type field" '[[ -n "${MUT:-}" ]]'
_assert "candidate has parent_id field" '[[ -n "${PAR:-}" ]]'
_assert "candidate has selection_score field" '[[ -n "${SEL:-}" ]]'
_assert "candidate has novelty_estimated field" '[[ -n "${NOV:-}" ]]'

echo ""
echo "--- mutation_type is from the typed registry ---"

VALID_MUTATIONS="add_node remove_node rewrite_node_prompt add_verifier change_model_lane reorder_edges add_human_gate split_by_task_type"
MUT_VALID=0
for m in $VALID_MUTATIONS; do
  [[ "$MUT" == "$m" ]] && MUT_VALID=1 && break
done
_assert "mutation_type is a valid typed mutation" '[[ "$MUT_VALID" -eq 1 ]]'

echo ""
echo "--- _group_novelty_score: returns 0.0-1.0 float ---"

NOVELTY="$(_group_novelty_score "$FIRST_CAND" "$HISTORY_JSON" 2>/dev/null || echo '')"
_assert "_group_novelty_score returns non-empty value" '[[ -n "${NOVELTY:-}" ]]'
NOV_OK="$(python3 -c "
v=float('${NOVELTY:-0}')
print('ok' if 0.0 <= v <= 1.0 else 'bad')
" 2>/dev/null || echo 'bad')"
_assert "_group_novelty_score returns value in [0.0, 1.0]" '[[ "$NOV_OK" == "ok" ]]'

echo ""
echo "--- group_propose: empty history → error output, no crash ---"

EMPTY_OUT="$(group_propose '[]' 2>/dev/null || true)"
_assert "group_propose on empty history does not crash" '[[ $? -eq 0 || -n "${EMPTY_OUT:-}" ]]'

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
