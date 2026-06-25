#!/usr/bin/env bash
# Smoke proof for Phase 0 learning loops. Uses only a copied/temp database.

set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TMP_DB="$(mktemp /tmp/mini-ork-learning-loop.XXXXXX.db)"
PASS=0
FAIL=0

cleanup() {
  rm -f "$TMP_DB" "$TMP_DB-wal" "$TMP_DB-shm"
}
trap cleanup EXIT

if [ -f "$MINI_ORK_ROOT/.mini-ork/state.db" ]; then
  cp "$MINI_ORK_ROOT/.mini-ork/state.db" "$TMP_DB"
else
  : > "$TMP_DB"
fi

export MINI_ORK_ROOT
export MINI_ORK_HOME="$(dirname "$TMP_DB")"
export MINI_ORK_DB="$TMP_DB"

bash "$MINI_ORK_ROOT/db/init.sh" >/tmp/mini-ork-learning-init.log 2>&1

assert_sql() {
  local desc="$1" sql="$2" expected="$3"
  local actual
  actual="$(sqlite3 "$TMP_DB" "$sql")"
  if [ "$actual" = "$expected" ]; then
    printf "PASS %s\n" "$desc"
    PASS=$((PASS + 1))
  else
    printf "FAIL %s\n  expected=%s\n  actual=%s\n" "$desc" "$expected" "$actual" >&2
    FAIL=$((FAIL + 1))
  fi
}

assert_ge() {
  local desc="$1" sql="$2" min="$3"
  local actual
  actual="$(sqlite3 "$TMP_DB" "$sql")"
  if [ "${actual:-0}" -ge "$min" ] 2>/dev/null; then
    printf "PASS %s\n" "$desc"
    PASS=$((PASS + 1))
  else
    printf "FAIL %s\n  expected >= %s\n  actual=%s\n" "$desc" "$min" "${actual:-}" >&2
    FAIL=$((FAIL + 1))
  fi
}

export MINI_ORK_EXECUTE_SOURCE_ONLY=1
# shellcheck source=../bin/mini-ork-execute
source "$MINI_ORK_ROOT/bin/mini-ork-execute"
unset MINI_ORK_EXECUTE_SOURCE_ONLY

TS="ll$(date +%s)-$$"
TC="learning_loop_$TS"
EPIC="learning-epic-$TS"

sqlite3 "$TMP_DB" <<SQL
INSERT INTO epics(id, title, status)
VALUES('$EPIC', 'learning loop smoke', 'done');

INSERT INTO conductor_decisions
  (decided_at, epic_id, task_class, chosen_topology, chosen_recipe,
   chosen_lane_hints, predicted_score, budget_pct_used, rationale, outcome)
VALUES
  (strftime('%s','now'), '$EPIC', '$TC', 'wf-smoke', 'framework-edit',
   '{"implementer":"cheap_lens"}', 0.4, 0.1, 'smoke seed', 'pending');

INSERT INTO prompt_win_rates
  (prompt_version_hash, task_class, node_type, wins, losses, ties, win_rate, sample_size)
VALUES
  ('prompt-smoke', '$TC', 'implementer', 4, 1, 0, 0.8, 5);

INSERT INTO execution_traces
  (trace_id, run_id, workflow_version_id, agent_version_id, task_class,
   prompt_version_hash, context_bundle_hash, tool_calls, files_read,
   files_written, verifier_output, reviewer_verdict, cost_usd,
   duration_ms, final_artifact_ref, status, process_reward)
VALUES
  ('tr-good-1-$TS', NULL, 'wf-smoke', 'frontier_lens', '$TC',
   'prompt-smoke', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '["a"]', '{"node_type":"implementer"}', 'APPROVE', 0.04,
   2000, 'artifact', 'success', 1.0),
  ('tr-good-2-$TS', NULL, 'wf-smoke', 'frontier_lens', '$TC',
   'prompt-smoke', 'ctx', '[{"tool":"Edit"}]', '["a"]',
   '["a"]', '{"node_type":"implementer"}', 'APPROVE', 0.05,
   2200, 'artifact', 'success', 0.9),
  ('tr-bad-1-$TS', NULL, 'wf-smoke', 'cheap_lens', '$TC',
   'prompt-smoke', 'ctx', '[]', '[]',
   '[]', '{"node_type":"implementer"}', 'REJECT', 0.01,
   300, NULL, 'failure', 0.1),
  ('tr-bad-2-$TS', NULL, 'wf-smoke', 'cheap_lens', '$TC',
   'prompt-smoke', 'ctx', '[]', '[]',
   '[]', '{"node_type":"implementer"}', 'REJECT', 0.01,
   300, NULL, 'failure', 0.2);

INSERT INTO gradient_records
  (gradient_id, target, signal, suggested_change, evidence, confidence, created_at, task_class)
VALUES
  ('grad-$TS', 'implementer', 'missing artifact gate',
   'require implementer_summary and diff before verifier', 'smoke', 0.9,
   strftime('%s','now'), '$TC');

INSERT INTO grounded_rejections
  (id, gate_name, verdict, concern, evidence_trace_ids, evidence_summary, suggestion)
VALUES
  ('gr-$TS', 'implementer', 'fail',
   'draft has proof artifacts but implementer_summary.json was missing',
   '["tr-bad-1-$TS"]', 'artifact-presence: impl.log',
   'require implementer_summary and diff before verifier');
SQL

mo_learning_update_conductor_outcomes >/tmp/mini-ork-learning-conductor.out
mo_learning_write_grpo_advantages >/tmp/mini-ork-learning-grpo.out

TASK_CLASS="$TC"
MO_ROUTING_POLICY=learning_governed
MO_LEARNING_MIN_SAMPLES=2
MO_LEARNING_EPSILON=0
ROUTED="$(_mo_learning_governed_lane implementer cheap_lens)"

assert_sql "process_reward column exists" \
  "SELECT COUNT(*) FROM pragma_table_info('execution_traces') WHERE name='process_reward';" "1"
assert_sql "relative_advantage column exists" \
  "SELECT COUNT(*) FROM pragma_table_info('agent_performance_memory') WHERE name='relative_advantage';" "1"
assert_ge "RHO prompt_win_rates has sufficient sample" \
  "SELECT sample_size FROM prompt_win_rates WHERE task_class='$TC' AND node_type='implementer';" "3"
assert_sql "PRM rewards were seeded" \
  "SELECT COUNT(*) FROM execution_traces WHERE task_class='$TC' AND process_reward IS NOT NULL;" "4"
assert_ge "GRPO writes agent performance rows" \
  "SELECT COUNT(*) FROM agent_performance_memory WHERE task_class='$TC';" "2"
assert_ge "GRPO relative advantage prefers frontier lane" \
  "SELECT CASE WHEN relative_advantage > 0 THEN 1 ELSE 0 END FROM agent_performance_memory WHERE task_class='$TC' AND agent_version_id='frontier_lens';" "1"
assert_sql "learning_governed routing uses learned frontier lane" \
  "SELECT '$ROUTED';" "frontier_lens"
assert_sql "conductor_decisions writeback marks success" \
  "SELECT outcome || ':' || printf('%.1f', realized_score) FROM conductor_decisions WHERE epic_id='$EPIC';" "success:1.0"
assert_ge "gradient_records table accepts learning signal" \
  "SELECT COUNT(*) FROM gradient_records WHERE task_class='$TC';" "1"
assert_ge "grounded_rejections records refuted draft" \
  "SELECT COUNT(*) FROM grounded_rejections WHERE id='gr-$TS' AND gate_name='implementer' AND verdict='fail' AND suggestion='require implementer_summary and diff before verifier';" "1"

if [ "$PASS" -eq 0 ]; then
  echo "FAIL zero assertions ran" >&2
  exit 1
fi

printf "SUMMARY %d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
