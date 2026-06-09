#!/usr/bin/env bash
# tests/unit/test_verifier_ref_json.sh — regression coverage for verifier_ref JSON verdicts.
# Usage: bash tests/unit/test_verifier_ref_json.sh
set -uo pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MINI_ORK_ROOT

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

PLAN_PATH="$TEST_DIR/plan.json"
ARTIFACT_PATH="$TEST_DIR/artifact.md"
export PLAN_PATH ARTIFACT_PATH
printf '{}\n' > "$PLAN_PATH"
printf 'artifact\n' > "$ARTIFACT_PATH"

export MINI_ORK_EXECUTE_SOURCE_ONLY=1
source "$MINI_ORK_ROOT/bin/mini-ork-execute"
unset MINI_ORK_EXECUTE_SOURCE_ONLY

_write_fixture() {
  local name="$1" body="$2"
  local path="$TEST_DIR/$name.sh"
  printf '%s\n' '#!/usr/bin/env bash' "$body" > "$path"
  chmod +x "$path"
  echo "$path"
}

_assert_pass() {
  local label="$1" script="$2"
  local evidence="$TEST_DIR/${label//[^A-Za-z0-9_]/_}.log"
  if _run_verifier_ref "$script" "$evidence"; then
    _ok "$label"
  else
    _fail "$label"
  fi
}

_assert_fail() {
  local label="$1" script="$2"
  local evidence="$TEST_DIR/${label//[^A-Za-z0-9_]/_}.log"
  if _run_verifier_ref "$script" "$evidence"; then
    _fail "$label"
  else
    _ok "$label"
  fi
}

echo "── unit: verifier_ref JSON adapter ──"

json_false=$(_write_fixture "json_false" "echo '{\"pass\": false}'; exit 0")
json_true=$(_write_fixture "json_true" "echo '{\"pass\": true}'; exit 0")
legacy_fail=$(_write_fixture "legacy_fail" "echo fail; exit 1")
legacy_ok=$(_write_fixture "legacy_ok" "echo ok; exit 0")

_assert_fail "json verifier with pass=false is rejected" "$json_false"
_assert_pass "json verifier with pass=true is accepted" "$json_true"
_assert_fail "legacy verifier with exit 1 and non-JSON stdout is rejected" "$legacy_fail"
_assert_pass "legacy verifier with exit 0 and non-JSON stdout is accepted" "$legacy_ok"

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ]
