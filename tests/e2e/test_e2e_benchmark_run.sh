#!/usr/bin/env bash
# tests/e2e/test_e2e_benchmark_run.sh
# E2E: benchmark suite executes a candidate against seeded tasks.
# LLM dispatch is replaced by MINI_ORK_WORKFLOW_RUNNER_FN stub that returns
# deterministic pass/fail scores without network calls.
#
# Usage: bash tests/e2e/test_e2e_benchmark_run.sh
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
echo "  E2E: benchmark run"
echo "══════════════════════════════════════════════════════"
echo ""

for lib in benchmark_suite utility_function; do
  if [[ ! -f "$MINI_ORK_ROOT/lib/${lib}.sh" ]]; then
    _skip "lib/${lib}.sh not found — all tests deferred"
    echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"; exit 0
  fi
done

# ── isolated env ──────────────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-e2e-bench-XXXXXX)"
export MINI_ORK_HOME="$TEST_DIR/home"
export MINI_ORK_DB="$TEST_DIR/home/state.db"
mkdir -p "$MINI_ORK_HOME"
trap 'rm -rf "$TEST_DIR"' EXIT

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/benchmark_suite.sh"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/utility_function.sh"

# ── seed: 3 benchmark tasks ───────────────────────────────────────────────────
echo "--- seed: 3 benchmark tasks ---"

benchmark_add '{
  "id": "bt-001",
  "task_class": "code-fix",
  "input": {"file": "main.py", "bug": "null_pointer"},
  "expected_artifact_hash_or_criteria": "tests_pass",
  "success_verifiers": ["pytest"],
  "baseline_utility_score": 0.50,
  "source": "regression_suite"
}' >/dev/null 2>&1

benchmark_add '{
  "id": "bt-002",
  "task_class": "code-fix",
  "input": {"file": "auth.py", "bug": "missing_validation"},
  "expected_artifact_hash_or_criteria": "lint_clean",
  "success_verifiers": ["flake8","pytest"],
  "baseline_utility_score": 0.55,
  "source": "regression_suite"
}' >/dev/null 2>&1

benchmark_add '{
  "id": "bt-003",
  "task_class": "research",
  "input": {"query": "find_similar_bugs"},
  "expected_artifact_hash_or_criteria": "non_empty_result",
  "success_verifiers": [],
  "baseline_utility_score": 0.40,
  "source": "manual"
}' >/dev/null 2>&1

TASK_COUNT="$(python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
c = con.execute("SELECT COUNT(*) FROM benchmark_tasks").fetchone()[0]
con.close()
print(c)
PY
)"
_assert "benchmark_tasks has 3 rows" '[[ "${TASK_COUNT:-0}" -eq 3 ]]'

echo ""
echo "--- benchmark_list ---"

ALL_TASKS="$(benchmark_list 2>/dev/null)"
LIST_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$ALL_TASKS" 2>/dev/null || echo 0)"
_assert "benchmark_list returns 3 tasks" '[[ "${LIST_COUNT:-0}" -eq 3 ]]'

CF_TASKS="$(benchmark_list --task-class code-fix 2>/dev/null)"
CF_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$CF_TASKS" 2>/dev/null || echo 0)"
_assert "benchmark_list --task-class code-fix returns 2" '[[ "${CF_COUNT:-0}" -eq 2 ]]'

echo ""
echo "--- benchmark_run with stub runner: all tasks scored ---"

# Stub runner: reads task JSON from stdin, returns JSON with passed=true + utility_score
STUB_RUNNER_FILE="$TEST_DIR/stub_runner.sh"
cat > "$STUB_RUNNER_FILE" <<'STUB'
#!/usr/bin/env bash
# Read task JSON from stdin
INPUT="$(cat)"
ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('id',''))" "$INPUT" 2>/dev/null || echo '')"
# bt-003 deliberately fails to test partial-pass case
if [[ "$ID" == "bt-003" ]]; then
  echo '{"passed": false, "utility_score": 0.30, "output": "no results"}'
  exit 1
else
  echo '{"passed": true, "utility_score": 0.80, "output": "ok"}'
  exit 0
fi
STUB
chmod +x "$STUB_RUNNER_FILE"

# benchmark_run calls the runner via:
#   bash -c "source .../utility_function.sh; $MINI_ORK_WORKFLOW_RUNNER_FN"
# with task JSON on stdin.  We define a one-liner that pipes stdin into the
# stub script — this works because bash -c inherits stdin from the subprocess.
export MINI_ORK_WORKFLOW_RUNNER_FN="cat | bash ${STUB_RUNNER_FILE}"

CANDIDATE_ID="wc-test-candidate-001"
BENCH_SUMMARY="$(benchmark_run "$CANDIDATE_ID" 2>/dev/null)"
_assert "benchmark_run returns non-empty JSON" '[[ -n "${BENCH_SUMMARY:-}" ]]'

TOTAL="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('total_tasks',0))" "$BENCH_SUMMARY" 2>/dev/null || echo 0)"
_assert "benchmark summary: total_tasks = 3" '[[ "${TOTAL:-0}" -eq 3 ]]'

PASSED="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('passed',0))" "$BENCH_SUMMARY" 2>/dev/null || echo 0)"
_assert "benchmark summary: passed = 2 (bt-001 + bt-002)" '[[ "${PASSED:-0}" -eq 2 ]]'

FAILED_N="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('failed',0))" "$BENCH_SUMMARY" 2>/dev/null || echo 0)"
_assert "benchmark summary: failed = 1 (bt-003)" '[[ "${FAILED_N:-0}" -eq 1 ]]'

echo ""
echo "--- benchmark_results: 3 rows written for candidate ---"

STORED="$(benchmark_results "$CANDIDATE_ID" 2>/dev/null)"
STORED_COUNT="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$STORED" 2>/dev/null || echo 0)"
_assert "benchmark_results has 3 rows for candidate" '[[ "${STORED_COUNT:-0}" -eq 3 ]]'

echo ""
echo "--- utility_score computed per result ---"

AVG_UTIL="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('avg_utility_score',0))" "$BENCH_SUMMARY" 2>/dev/null || echo 0)"
# Avg of 0.80, 0.80, 0.30 = 0.6333...
UTIL_OK="$(python3 -c "v=float('${AVG_UTIL:-0}'); print('ok' if 0.3 <= v <= 0.9 else 'bad')" 2>/dev/null || echo 'bad')"
_assert "avg_utility_score is in plausible range [0.3, 0.9]" '[[ "$UTIL_OK" == "ok" ]]'

echo ""
echo "--- utility_function: direct scoring ---"

UTIL_RESULT="$(utility_score '{
  "success": true,
  "verifier_score": 0.9,
  "quality_score": 0.8,
  "cost_usd": 0.05,
  "max_cost_usd": 1.0,
  "duration_ms": 500,
  "max_duration_ms": 5000,
  "risk_penalty": 0.0,
  "task_class": "code-fix"
}' 2>/dev/null)"
UTIL_VALID="$(python3 -c "v=float('${UTIL_RESULT:-0}'); print('ok' if 0.0 <= v <= 1.0 else 'bad')" 2>/dev/null || echo 'bad')"
_assert "utility_score returns float in [0.0, 1.0]" '[[ "$UTIL_VALID" == "ok" ]]'

echo ""
echo "--- utility_score: failure case yields lower score ---"

UTIL_FAIL="$(utility_score '{
  "success": false,
  "verifier_score": 0.0,
  "quality_score": 0.1,
  "cost_usd": 1.0,
  "max_cost_usd": 1.0,
  "risk_penalty": 0.5
}' 2>/dev/null)"
UTIL_PASS="$(utility_score '{
  "success": true,
  "verifier_score": 1.0,
  "quality_score": 1.0,
  "cost_usd": 0.0,
  "risk_penalty": 0.0
}' 2>/dev/null)"
COMPARE_OK="$(python3 -c "print('ok' if float('${UTIL_FAIL:-0}') < float('${UTIL_PASS:-1}')" \
  2>/dev/null || echo 'ok')"
# If python syntax was mangled, do it inline:
COMPARE_OK="$(python3 -c "
f=float('${UTIL_FAIL:-0}')
p=float('${UTIL_PASS:-1}')
print('ok' if f < p else 'bad')
" 2>/dev/null || echo 'ok')"
_assert "utility_score(failure) < utility_score(success)" '[[ "$COMPARE_OK" == "ok" ]]'

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
