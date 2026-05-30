#!/usr/bin/env bash
# tests/unit/test_artifact_contract.sh — unit tests for lib/artifact_contract.sh
# Usage: bash tests/unit/test_artifact_contract.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/artifact_contract.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: artifact_contract.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/artifact_contract.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -rf "$MINI_ORK_HOME"' EXIT

# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: artifact_contract_load returns default when no file exists ---"

CONTRACT="$(artifact_contract_load "nonexistent-task-class" 2>/dev/null)"
DEFAULT_FLAG="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('_default',''))" "$CONTRACT" 2>/dev/null || echo "")"
if [[ "$DEFAULT_FLAG" == "True" || "$DEFAULT_FLAG" == "true" ]]; then
  _ok "artifact_contract_load returns default contract when no file"
else
  _fail "artifact_contract_load default flag not set: _default='$DEFAULT_FLAG'"
fi

FAIL_POL="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('failure_policy',''))" "$CONTRACT" 2>/dev/null || echo "")"
_assert_eq "default contract has failure_policy=escalate" "$FAIL_POL" "escalate"

echo ""
echo "--- happy path: artifact_contract_load reads a YAML contract file ---"

mkdir -p "$MINI_ORK_HOME/config/artifact_contracts"
cat > "$MINI_ORK_HOME/config/artifact_contracts/test-patch.yaml" <<'YAML'
task_type: test-patch
expected_artifact: patch
failure_policy: request_changes
rollback_policy: auto
success_verifiers: []
YAML

CONTRACT2="$(artifact_contract_load "test-patch" 2>/dev/null)"
EXPECTED="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('expected_artifact',''))" "$CONTRACT2" 2>/dev/null || echo "")"
_assert_eq "artifact_contract_load reads expected_artifact from YAML" "$EXPECTED" "patch"

FAIL_POL2="$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('failure_policy',''))" "$CONTRACT2" 2>/dev/null || echo "")"
_assert_eq "artifact_contract_load reads failure_policy from YAML" "$FAIL_POL2" "request_changes"

echo ""
echo "--- happy path: artifact_contract_validate passes for correct file type ---"

PATCH_FILE="$(mktemp /tmp/test-artifact-XXXXXX.patch)"
echo "--- a/file.txt +++ b/file.txt @@ -1 +1 @@ -old +new" > "$PATCH_FILE"

RESULT="$(artifact_contract_validate "test-patch" "$PATCH_FILE" 2>/dev/null)"
VERDICT="$(echo "$RESULT" | head -1)"
_assert_eq "artifact_contract_validate passes for .patch file" "$VERDICT" "pass"
rm -f "$PATCH_FILE"

echo ""
echo "--- happy path: artifact_contract_validate fails for wrong file type ---"

JSON_FILE="$(mktemp /tmp/test-artifact-XXXXXX.json)"
echo '{"data":"yes"}' > "$JSON_FILE"

RESULT2="$(artifact_contract_validate "test-patch" "$JSON_FILE" 2>/dev/null)"
VERDICT2="$(echo "$RESULT2" | head -1)"
_assert_eq "artifact_contract_validate fails for .json when patch expected" "$VERDICT2" "fail"
rm -f "$JSON_FILE"

echo ""
echo "--- edge case: artifact_contract_validate fails when artifact path does not exist ---"

RESULT3="$(artifact_contract_validate "test-patch" "/tmp/no-such-artifact-xyz.patch" 2>/dev/null)"
VERDICT3="$(echo "$RESULT3" | head -1)"
_assert_eq "artifact_contract_validate fails for missing artifact" "$VERDICT3" "fail"

echo ""
echo "--- error path: artifact_contract_load with missing task_class arg exits non-zero ---"

if bash -c "source '$LIB' 2>/dev/null; artifact_contract_load" >/dev/null 2>&1; then
  _fail "artifact_contract_load with no args should exit non-zero"
else
  _ok "artifact_contract_load with no args exits non-zero"
fi

echo ""
echo "--- error path: artifact_contract_validate with missing args exits non-zero ---"

if bash -c "source '$LIB' 2>/dev/null; artifact_contract_validate" >/dev/null 2>&1; then
  _fail "artifact_contract_validate with no args should exit non-zero"
else
  _ok "artifact_contract_validate with no args exits non-zero"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
