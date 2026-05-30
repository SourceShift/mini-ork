#!/usr/bin/env bash
# tests/unit/test_utility_function.sh — unit tests for lib/utility_function.sh
# Usage: bash tests/unit/test_utility_function.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/utility_function.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

echo "── unit: utility_function.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/utility_function.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -rf "$MINI_ORK_HOME"' EXIT

# shellcheck source=/dev/null
source "$LIB"

_float_near() {
  # Returns 0 (true) if |got - want| <= tolerance
  local got="$1" want="$2" tol="${3:-0.001}"
  python3 -c "import sys; sys.exit(0 if abs(float('$got') - float('$want')) <= float('$tol') else 1)" 2>/dev/null
}

echo ""
echo "--- happy path: perfect run (success=1, verifier=1, quality=1) scores near max ---"

SCORE="$(utility_score '{"success":1,"verifier_score":1.0,"quality_score":1.0,"cost_usd":0,"duration_ms":0,"risk_penalty":0}' 2>/dev/null)"
# Expected: 0.45*1 + 0.20*1 + 0.15*1 - 0 - 0 - 0 = 0.80
if _float_near "$SCORE" "0.80" "0.01"; then
  _ok "perfect run utility score ≈ 0.80 (got $SCORE)"
else
  _fail "perfect run utility score wrong: $SCORE (expected ~0.80)"
fi

echo ""
echo "--- happy path: failed run (success=0) scores lower ---"

FAIL_SCORE="$(utility_score '{"success":0,"verifier_score":0.0,"quality_score":0.5,"cost_usd":0,"duration_ms":0,"risk_penalty":0}' 2>/dev/null)"
# Expected: 0.45*0 + 0.20*0 + 0.15*0.5 - 0 - 0 - 0 = 0.075
if _float_near "$FAIL_SCORE" "0.075" "0.01"; then
  _ok "failed run utility score ≈ 0.075 (got $FAIL_SCORE)"
else
  _fail "failed run utility score wrong: $FAIL_SCORE (expected ~0.075)"
fi

echo ""
echo "--- happy path: cost penalty reduces score ---"

# Max cost = 1, actual cost = 1 → full cost penalty 0.10
COST_SCORE="$(utility_score '{"success":1,"verifier_score":0.0,"quality_score":0.5,"cost_usd":1.0,"max_cost_usd":1.0,"risk_penalty":0,"duration_ms":0}' 2>/dev/null)"
# Expected: 0.45*1 + 0.20*0 + 0.15*0.5 - 0.10*1.0 - 0 - 0 = 0.45 + 0.075 - 0.10 = 0.425
if _float_near "$COST_SCORE" "0.425" "0.01"; then
  _ok "cost penalty applied correctly (got $COST_SCORE, expected ~0.425)"
else
  _fail "cost penalty wrong: $COST_SCORE (expected ~0.425)"
fi

echo ""
echo "--- happy path: result is always clamped to [0.0, 1.0] ---"

HIGH="$(utility_score '{"success":1,"verifier_score":1,"quality_score":1,"cost_usd":0,"risk_penalty":0}' 2>/dev/null)"
if python3 -c "import sys; v=float('$HIGH'); sys.exit(0 if 0.0<=v<=1.0 else 1)" 2>/dev/null; then
  _ok "utility score clamped to [0,1]: $HIGH"
else
  _fail "utility score out of [0,1]: $HIGH"
fi

# Pathological: big risk penalty
LOW="$(utility_score '{"success":0,"verifier_score":0,"quality_score":0,"cost_usd":0,"risk_penalty":1}' 2>/dev/null)"
if python3 -c "import sys; v=float('$LOW'); sys.exit(0 if 0.0<=v<=1.0 else 1)" 2>/dev/null; then
  _ok "utility score clamped at 0 with all penalties: $LOW"
else
  _fail "utility score out of [0,1] with penalties: $LOW"
fi

echo ""
echo "--- happy path: per-class override is invoked when override script present ---"

mkdir -p "$MINI_ORK_HOME/config/utility_functions"
cat > "$MINI_ORK_HOME/config/utility_functions/override-test.sh" <<'OVERRIDE'
utility_score_override() {
  echo "0.999999"
}
OVERRIDE

OVERRIDE_SCORE="$(utility_score '{"task_class":"override-test","success":0}' 2>/dev/null)"
if _float_near "$OVERRIDE_SCORE" "0.999999" "0.000001"; then
  _ok "per-class override script invoked (got $OVERRIDE_SCORE)"
else
  _fail "per-class override not invoked (got $OVERRIDE_SCORE, expected 0.999999)"
fi

echo ""
echo "--- edge case: string 'true' counts as success=1 ---"

STR_SCORE="$(utility_score '{"success":"true","verifier_score":0,"quality_score":0}' 2>/dev/null)"
if python3 -c "import sys; sys.exit(0 if float('$STR_SCORE') >= 0.4 else 1)" 2>/dev/null; then
  _ok "success='true' (string) treated as 1 (score=$STR_SCORE)"
else
  _fail "success='true' not treated as 1 (score=$STR_SCORE)"
fi

echo ""
echo "--- error path: utility_score with invalid JSON exits non-zero ---"

if utility_score "not-json" >/dev/null 2>&1; then
  _fail "utility_score with invalid JSON should exit non-zero"
else
  _ok "utility_score with invalid JSON exits non-zero"
fi

echo ""
echo "--- error path: utility_score with missing required arg exits non-zero ---"

# Use bash -c subshell to isolate set -e / trap from this test's shell
if bash -c "source '$LIB' 2>/dev/null; utility_score" >/dev/null 2>&1; then
  _fail "utility_score with no args should exit non-zero"
else
  _ok "utility_score with no args exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
