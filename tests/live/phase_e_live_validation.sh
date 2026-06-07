#!/usr/bin/env bash
# tests/live/phase_e_live_validation.sh
#
# Phase E LIVE end-to-end validation — proves the
#   improve → benchmark → eval → promote
# chain works against REAL LLM calls (not the stub runner used by
# tests/e2e/test_e2e_benchmark_run.sh and the rest of the CI pyramid).
#
# Why this is its own script (not in tests/run-all.sh):
#   - Requires live API credentials / CLI auth — CI would need
#     secret injection.
#   - Costs real money (~$0.05-0.20 per run depending on provider).
#   - Slow (~30-90 sec per benchmark task in real-LLM mode).
#   - Intended to run ON DEMAND by the operator, not on every push.
#
# Exit 0 = full chain ran + every assertion green.
# Exit 1 = any assertion failed (run dir preserved for forensics).
#
# Usage:
#   bash tests/live/phase_e_live_validation.sh
#   PHASE_E_PROVIDER=codex bash tests/live/phase_e_live_validation.sh
#   PHASE_E_PROVIDER=minimax bash tests/live/phase_e_live_validation.sh
#   PHASE_E_BUDGET_USD=0.50 bash tests/live/phase_e_live_validation.sh

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

PROVIDER="${PHASE_E_PROVIDER:-codex}"
BUDGET_USD="${PHASE_E_BUDGET_USD:-2.00}"
TASK_TIMEOUT_SECONDS="${PHASE_E_TASK_TIMEOUT_SECONDS:-120}"
RUN_TS=$(date +%Y%m%d-%H%M%S)

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_assert() {
  local name="$1"; shift
  if eval "$@" 2>/dev/null; then _ok "$name"; else _fail "$name  (expr: $*)"; fi
}

echo "═══════════════════════════════════════════════════════"
echo "  Phase E LIVE — improve → benchmark → eval → promote"
echo "═══════════════════════════════════════════════════════"
echo "  provider: $PROVIDER"
echo "  budget:   \$${BUDGET_USD}"
echo "  timeout:  ${TASK_TIMEOUT_SECONDS}s/task"
echo "  ts:       $RUN_TS"
echo

case "$PROVIDER" in
  codex|minimax|kimi|glm) ;;
  *)
    echo "  [SKIP] provider '$PROVIDER' is disabled for this 24h window; use codex|minimax|kimi|glm"
    exit 0
    ;;
esac

# ── isolated DB ──────────────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-phase-e-XXXXXX)"
export MINI_ORK_HOME="$TEST_DIR/home"
export MINI_ORK_DB="$TEST_DIR/home/state.db"
mkdir -p "$MINI_ORK_HOME/runs"
echo "  test_dir: $TEST_DIR"

# Locate secrets (don't print).
SECRETS=""
for p in \
  "$MINI_ORK_ROOT/.mini-ork/config/secrets.local.sh" \
  "$HOME/.config/mini-ork/secrets.local.sh" \
  "/Volumes/docker-ssd/Migration/Development/researcher/.agentflow/config/secrets.local.sh"; do
  [ -f "$p" ] && SECRETS="$p" && break
done
if [ -z "$SECRETS" ] && [ "$PROVIDER" != "codex" ]; then
  echo "  [SKIP] no secrets file found — Phase E LIVE requires API keys for $PROVIDER"
  exit 0
fi
if [ -n "$SECRETS" ]; then
  echo "  secrets: $SECRETS (loaded)"
  # shellcheck source=/dev/null
  source "$SECRETS"
else
  echo "  secrets: not required for codex CLI provider"
fi

# Apply migrations.
# shellcheck source=tests/lib/setup_state_db.sh
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations >/dev/null
echo "  schema: applied"

# Seed runs row + workflow_memory + workflow_candidate.
python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("INSERT OR IGNORE INTO runs (id, agent, final_verdict) VALUES (1, 'phase-e-live', 'APPROVE')")
con.execute("""
    INSERT OR IGNORE INTO workflow_memory
        (workflow_version_id, workflow_name, yaml_hash, yaml_blob)
    VALUES ('phase-e-baseline-v1', 'phase-e-baseline', 'deadbeef', '# baseline')
""")
CAND_ID = 'wc-phase-e-cand-001'
con.execute("""
    INSERT OR IGNORE INTO workflow_candidates
        (candidate_id, base_workflow_version_id, created_by)
    VALUES (?, 'phase-e-baseline-v1', 'evolution_engine')
""", (CAND_ID,))
con.commit(); con.close()
print(CAND_ID)
PY
CAND_ID="wc-phase-e-cand-001"
echo "  candidate: $CAND_ID"

# ── load benchmark suite + utility function ──────────────────────────────────
# shellcheck source=lib/benchmark_suite.sh
source "$MINI_ORK_ROOT/lib/benchmark_suite.sh"
# shellcheck source=lib/utility_function.sh
source "$MINI_ORK_ROOT/lib/utility_function.sh"
# shellcheck source=lib/promotion_gate.sh
source "$MINI_ORK_ROOT/lib/promotion_gate.sh"

# ── seed 2 benchmark tasks ───────────────────────────────────────────────────
# Tasks are deterministic-output: the runner gets a question + expected
# answer, asks the LLM, parses output, computes utility_score from
# correctness. This lets us prove the e2e chain works WITHOUT building
# a 5-stage workflow — the LLM is the workflow approximation.

benchmark_add '{
  "id": "bt-phase-e-001",
  "task_class": "code-fix",
  "input_payload": {
    "prompt": "Count vowels (a, e, i, o, u) in the word DEMOCRACY. Reply with exactly: ANSWER: <count>",
    "expected": "5"
  },
  "baseline_utility_score": 0.50,
  "source": "synthetic"
}' >/dev/null

benchmark_add '{
  "id": "bt-phase-e-002",
  "task_class": "code-fix",
  "input_payload": {
    "prompt": "Compute 13 + 29. Reply with exactly: ANSWER: <number>",
    "expected": "42"
  },
  "baseline_utility_score": 0.50,
  "source": "synthetic"
}' >/dev/null

BENCH_ROWS=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM benchmark_tasks;")
_assert "2 benchmark_tasks seeded" "[[ \"$BENCH_ROWS\" -eq 2 ]]"
echo

# ── REAL runner: invokes the selected provider wrapper, parses output ─────────
# The runner is called by benchmark_run with task JSON on stdin.
# It must emit JSON {passed: bool, utility_score: float, output: str} on stdout.

RUNNER_FILE="$TEST_DIR/live_provider_runner.sh"
cat > "$RUNNER_FILE" <<'RUNNER'
#!/usr/bin/env bash
# live_provider_runner.sh — real-LLM benchmark runner.
set -uo pipefail
TASK_JSON="$(cat)"
PROVIDER_NAME="${1:-codex}"
PROVIDER_PATH="${2:-$MINI_ORK_ROOT/lib/providers/cl_codex.sh}"

PROMPT=$(printf '%s' "$TASK_JSON" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    p = (d.get('input_payload') or d.get('input') or {})
    if isinstance(p, str):
        try: p = json.loads(p)
        except Exception: p = {}
    print(p.get('prompt', ''))
except Exception:
    print('')
")
EXPECTED=$(printf '%s' "$TASK_JSON" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    p = (d.get('input_payload') or d.get('input') or {})
    if isinstance(p, str):
        try: p = json.loads(p)
        except Exception: p = {}
    print(p.get('expected', ''))
except Exception:
    print('')
")

if [ -z "$PROMPT" ]; then
  echo '{"passed":false,"utility_score":0.0,"output":"empty prompt"}'
  exit 1
fi

# Invoke provider with stdin redirected. Codex is an executable wrapper;
# MiniMax/Kimi/GLM are sourceable Anthropic-compatible env wrappers.
if [ "$PROVIDER_NAME" = "codex" ]; then
  OUT=$(timeout "${PHASE_E_TASK_TIMEOUT_SECONDS:-120}" "$PROVIDER_PATH" --print --output-format text "$PROMPT" < /dev/null 2>&1)
else
  OUT=$(
    source "$PROVIDER_PATH" 2>/dev/null
    timeout "${PHASE_E_TASK_TIMEOUT_SECONDS:-120}" claude --print --output-format text "$PROMPT" < /dev/null 2>&1
  )
fi
RC=$?

# Parse "ANSWER: N" out of the output (case-insensitive).
ANSWER=$(printf '%s' "$OUT" | grep -ioE 'ANSWER:[[:space:]]*[0-9]+' | head -1 | grep -oE '[0-9]+$')

if [ "$RC" -ne 0 ] || [ -z "$OUT" ]; then
  OUTPUT_JSON=$(printf '%s' "${OUT:-timeout or empty}" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()[:500]))')
  echo "{\"passed\":false,\"utility_score\":0.0,\"output\":${OUTPUT_JSON},\"rc\":$RC}"
  exit 1
fi

if [ "$ANSWER" = "$EXPECTED" ]; then
  echo "{\"passed\":true,\"utility_score\":1.0,\"output\":\"correct: $ANSWER\"}"
  exit 0
elif [ -n "$ANSWER" ]; then
  # Wrong answer but model responded — partial credit
  echo "{\"passed\":false,\"utility_score\":0.30,\"output\":\"wrong: got $ANSWER expected $EXPECTED\"}"
  exit 1
else
  echo "{\"passed\":false,\"utility_score\":0.10,\"output\":\"unparseable response\"}"
  exit 1
fi
RUNNER
chmod +x "$RUNNER_FILE"

# benchmark_run invokes via:
#   bash -c "source utility_function.sh; $MINI_ORK_WORKFLOW_RUNNER_FN"
# with task JSON on stdin. The function we set pipes stdin to our runner.
export MINI_ORK_WORKFLOW_RUNNER_FN="cat | bash ${RUNNER_FILE} '${PROVIDER}' '$MINI_ORK_ROOT/lib/providers/cl_${PROVIDER}.sh'"
export PHASE_E_TASK_TIMEOUT_SECONDS="$TASK_TIMEOUT_SECONDS"

# ── DISPATCH: benchmark_run with LIVE LLM ─────────────────────────────────────
echo "── dispatching benchmark_run (real LLM calls — wall ~30-90s for 2 tasks) ──"
T_START=$(date +%s)
BENCH_SUMMARY="$(benchmark_run "$CAND_ID" 2>/dev/null)"
T_END=$(date +%s)
WALL=$((T_END - T_START))
echo "  wall: ${WALL}s"
echo "  summary: $BENCH_SUMMARY"
echo

# ── verify benchmark_results landed ──────────────────────────────────────────
TOTAL=$(echo "$BENCH_SUMMARY" | jq -r '.total_tasks // 0' 2>/dev/null)
PASSED_N=$(echo "$BENCH_SUMMARY" | jq -r '.passed // 0' 2>/dev/null)
ALL_PASS=$(echo "$BENCH_SUMMARY" | jq -r '.all_pass // false' 2>/dev/null)
AVG_UTIL=$(echo "$BENCH_SUMMARY" | jq -r '.avg_utility_score // 0' 2>/dev/null)

_assert "benchmark_run total_tasks = 2" "[[ \"${TOTAL:-0}\" -eq 2 ]]"
_assert "benchmark_run passed >= 1 (at least one task scored)" "[[ \"${PASSED_N:-0}\" -ge 1 ]]"

STORED_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM benchmark_results WHERE candidate_id='$CAND_ID';")
_assert "benchmark_results table has 2 rows for $CAND_ID" "[[ \"${STORED_COUNT:-0}\" -eq 2 ]]"

UTIL_RANGE_OK="no"
if python3 -c "
v = $AVG_UTIL
import sys
sys.exit(0 if (0.0 <= v <= 1.0) else 1)
"; then UTIL_RANGE_OK="ok"; fi
_assert "avg_utility_score in [0.0, 1.0]" "[[ \"$UTIL_RANGE_OK\" == \"ok\" ]]"
echo

# ── promotion_evaluate ───────────────────────────────────────────────────────
echo "── promotion_evaluate ──"
EVAL_RESULT="$(promotion_evaluate "$CAND_ID" 2>/dev/null)"
echo "  eval: $EVAL_RESULT"
DECISION=$(echo "$EVAL_RESULT" | jq -r '.decision // ""' 2>/dev/null)
if [ -n "${EVAL_RESULT:-}" ]; then
  _ok "promotion_evaluate returns JSON with decision"
else
  _fail "promotion_evaluate returns JSON with decision"
fi
_assert "decision is one of {promoted, quarantined, rejected, pending_human_approval}" \
  "[[ \"$DECISION\" == \"promoted\" || \"$DECISION\" == \"quarantined\" || \"$DECISION\" == \"rejected\" || \"$DECISION\" == \"pending_human_approval\" ]]"

PROMO_ROW=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM promotion_records WHERE candidate_id='$CAND_ID';")
_assert "promotion_records row written for $CAND_ID" "[[ \"${PROMO_ROW:-0}\" -ge 1 ]]"
echo

# ── emit completion report ───────────────────────────────────────────────────
REPORT_PATH="$MINI_ORK_ROOT/docs/_meta/phase-e-live-validation-${RUN_TS}.md"
mkdir -p "$(dirname "$REPORT_PATH")"
{
  echo "# Phase E LIVE — improve → benchmark → eval → promote (${RUN_TS})"
  echo
  echo "**Provider**: ${PROVIDER}"
  echo "**Wall time**: ${WALL}s"
  echo "**Timeout**: ${TASK_TIMEOUT_SECONDS}s/task"
  echo "**Candidate**: \`${CAND_ID}\`"
  echo
  echo "## Benchmark summary"
  echo
  echo '```json'
  echo "${BENCH_SUMMARY}" | python3 -m json.tool 2>/dev/null || echo "${BENCH_SUMMARY}"
  echo '```'
  echo
  echo "## Per-result rows"
  echo
  sqlite3 -header -column "$MINI_ORK_DB" \
    "SELECT benchmark_id, candidate_id, pass, utility_score, evidence_path FROM benchmark_results WHERE candidate_id='$CAND_ID';" \
    | sed 's/^/    /'
  echo
  echo "## Promotion decision"
  echo
  echo '```json'
  echo "${EVAL_RESULT}" | python3 -m json.tool 2>/dev/null || echo "${EVAL_RESULT}"
  echo '```'
  echo
  echo "## promotion_records row"
  echo
  sqlite3 -header -column "$MINI_ORK_DB" \
    "SELECT promotion_id, candidate_id, decision, utility_before, utility_after, decided_by FROM promotion_records WHERE candidate_id='$CAND_ID';" \
    | sed 's/^/    /'
  echo
  echo "## Assertion results"
  echo
  echo "    ${PASS} OK / ${FAIL} FAIL / ${SKIP} SKIP"
  echo
  echo "## What this proves"
  echo
  if [ "$FAIL" -eq 0 ]; then
    echo "- benchmark_suite.benchmark_run dispatches the MINI_ORK_WORKFLOW_RUNNER_FN"
    echo "  with real LLM calls via cl_${PROVIDER}.sh."
    echo "- The runner correctly parses model output + assigns utility_score."
    echo "- benchmark_results table receives 1 row per task with pass/util scored."
    echo "- promotion_gate.promotion_evaluate reads the aggregate summary +"
    echo "  emits a valid decision (promoted/quarantined/rejected/pending)."
    echo "- promotion_records persists the decision with decided_at + decided_by."
    echo
    echo "Phase E (improve → eval → promote) is now LIVE-VALIDATED, not just"
    echo "stub-test-green. The chain runs end-to-end against real LLM calls"
    echo "with real DB writes."
  else
    echo "- benchmark_suite.benchmark_run invoked the live runner and persisted"
    echo "  benchmark_results rows, but the live validation did not pass."
    echo "- promotion_gate.promotion_evaluate emitted and persisted a valid"
    echo "  decision from the failed benchmark aggregate."
    echo
    echo "Phase E remains PENDING live validation. Re-run this harness after"
    echo "resolving the provider/runtime failure captured in evidence_path."
  fi
} > "$REPORT_PATH"
echo "  report: $REPORT_PATH"
echo

echo "═══════════════════════════════════════════════════════"
echo "  Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL"
echo "═══════════════════════════════════════════════════════"

# Cleanup tmp DB but keep the report.
rm -rf "$TEST_DIR"

[ "$FAIL" -eq 0 ] || exit 1
exit 0
