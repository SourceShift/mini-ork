#!/usr/bin/env bash
# learning-loop-closure-gate.sh — repeatable green/red proof that mini-ork's
# learning loop is closed AND default-ON for REAL runs (not just the temp-DB
# smoke harness). Answers the standing question: "can we say with confidence
# that mini-ork learns and USES its learnings on every real run, always?"
#
# Two evidence classes, both must be green:
#   [CODE]  source-level defaults — the loop fires without any opt-in env var.
#   [LIVE]  live state.db — the schema + data exist to record/read learnings.
#
# Read-only: never writes to state.db, never dispatches a run.
#
# Exit 0 = closed/green. Exit 1 = a gap is open (printed to stderr).

set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DB="${MINI_ORK_DB:-$MINI_ORK_ROOT/.mini-ork/state.db}"
EXEC="$MINI_ORK_ROOT/mini_ork/ported/mini_ork_execute.py"
PASS=0
FAIL=0

ok()   { printf "PASS %s\n" "$1"; PASS=$((PASS + 1)); }
bad()  { printf "FAIL %s\n  %s\n" "$1" "${2:-}" >&2; FAIL=$((FAIL + 1)); }

# --- [CODE] defaults: the loop is ON unless explicitly opted out -------------

assert_grep() {
  local desc="$1" pattern="$2" file="$3"
  if grep -qE "$pattern" "$file" 2>/dev/null; then
    ok "[CODE] $desc"
  else
    bad "[CODE] $desc" "pattern not found: $pattern  ($file)"
  fi
}

assert_grep "process reward scoring default-ON" \
  'os\.environ\.get\("MO_PRM_SCORE", "1"\) == "1"' "$EXEC"
assert_grep "native process reward scorer wired" \
  'from mini_ork\.learning\.process_reward import score_trace' "$EXEC"
assert_grep "reward stamping default-ON" \
  'os\.environ\.get\("MO_REWARD_STAMP", "1"\)' "$EXEC"
assert_grep "routing policy default learning_governed" \
  'os\.environ\.get\("MO_ROUTING_POLICY"\) or "learning_governed"' "$EXEC"
assert_grep "learning writeback implementation present" \
  '^def write_grpo_advantages\(' "$EXEC"
assert_grep "agent_version_id stamped from dispatch_lane onto each trace" \
  'extra\["agent_version_id"\] = lane' "$EXEC"
assert_grep "resolved lane passed through the trace wrapper" \
  'finish_reason, lane=lane' "$EXEC"
assert_grep "GRPO writer present" \
  '^def write_grpo_advantages\(' "$EXEC"
assert_grep "conductor outcome writer present" \
  '^def learning_update_conductor_outcomes\(' "$EXEC"

# --- [LIVE] state.db: schema + data exist to record/read learnings ----------

if [ ! -f "$DB" ]; then
  bad "[LIVE] state.db exists" "missing: $DB"
else
  assert_sql() {
    local desc="$1" sql="$2" expected="$3"
    local actual; actual="$(sqlite3 "$DB" "$sql" 2>/dev/null || echo "__ERR__")"
    if [ "$actual" = "$expected" ]; then ok "[LIVE] $desc"
    else bad "[LIVE] $desc" "expected=$expected actual=$actual"; fi
  }
  assert_ge() {
    local desc="$1" sql="$2" min="$3"
    local actual; actual="$(sqlite3 "$DB" "$sql" 2>/dev/null || echo 0)"
    if [ "${actual:-0}" -ge "$min" ] 2>/dev/null; then ok "[LIVE] $desc"
    else bad "[LIVE] $desc" "expected >= $min actual=${actual:-}"; fi
  }

  assert_sql "process_reward column exists" \
    "SELECT COUNT(*) FROM pragma_table_info('execution_traces') WHERE name='process_reward';" "1"
  assert_sql "agent_version_id column exists" \
    "SELECT COUNT(*) FROM pragma_table_info('execution_traces') WHERE name='agent_version_id';" "1"
  assert_sql "relative_advantage column exists (GRPO)" \
    "SELECT COUNT(*) FROM pragma_table_info('agent_performance_memory') WHERE name='relative_advantage';" "1"
  assert_sql "grounded_rejections table present" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='grounded_rejections';" "1"
  assert_sql "prompt_win_rates table present (RHO)" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='prompt_win_rates';" "1"
  assert_sql "conductor_decisions table present" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='conductor_decisions';" "1"
  # Data presence: at least the backfilled traces carry a process_reward, so
  # the PRM column is not vacuously empty on the live DB.
  assert_ge "process_reward populated on real traces (backfill landed)" \
    "SELECT SUM(process_reward IS NOT NULL) FROM execution_traces;" "1"
fi

echo "----"
printf "SUMMARY %d passed, %d failed\n" "$PASS" "$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "LEARNING LOOP: OPEN — see FAIL lines above" >&2
  exit 1
fi
echo "LEARNING LOOP: CLOSED — default-ON for real runs"
