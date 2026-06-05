#!/usr/bin/env bash
# tests/unit/test_cw_por.sh — unit tests for lib/cw_por.sh
# Usage: bash tests/unit/test_cw_por.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
#
# Covers `mo_compute_cw_por` (Agarwal & Khanna 2025, arxiv:2504.00374):
#   - panel_healthy when CW-POR ≤ threshold
#   - authority_capture_suspected when CW-POR > threshold
#   - indeterminate when no ground_truth_match signal exists
#   - rc=2 on malformed verdict JSON
#   - threshold tunable via MO_CW_POR_THRESHOLD
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/cw_por.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: cw_por.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/cw_por.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TD=$(mktemp -d)
trap 'rm -rf "$TD"' EXIT

# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: clean panel (CW-POR=0) → panel_healthy ---"
cat > "$TD/clean.json" <<'JSON'
{
  "voters": [
    {"voter_id":"glm",    "vote":"approve","confidence":0.85,"ground_truth_match":true},
    {"voter_id":"kimi",   "vote":"approve","confidence":0.80,"ground_truth_match":true},
    {"voter_id":"codex",  "vote":"approve","confidence":0.75,"ground_truth_match":true},
    {"voter_id":"minimax","vote":"reject", "confidence":0.30,"ground_truth_match":false}
  ]
}
JSON
out_a=$(mo_compute_cw_por "$TD/clean.json")
_assert_eq "verdict=panel_healthy"      "$(echo "$out_a" | jq -r .verdict)" "panel_healthy"
# cw_por is round(_, 4) — python rounds 0.0 to 0.0, jq -r emits "0" for a JSON
# 0 but "0.0" for a JSON 0.0. Library emits 0.0 (float); compare verbatim.
_assert_eq "cw_por=0.0 when no override"  "$(echo "$out_a" | jq -r .cw_por)"  "0.0"

echo ""
echo "--- failure mode: captured panel (CW-POR>threshold) → authority_capture_suspected ---"
cat > "$TD/captured.json" <<'JSON'
{
  "voters": [
    {"voter_id":"glm",    "vote":"approve","confidence":0.40,"ground_truth_match":true},
    {"voter_id":"kimi",   "vote":"reject", "confidence":0.95,"ground_truth_match":false},
    {"voter_id":"codex",  "vote":"reject", "confidence":0.90,"ground_truth_match":false},
    {"voter_id":"minimax","vote":"reject", "confidence":0.85,"ground_truth_match":false}
  ]
}
JSON
out_b=$(mo_compute_cw_por "$TD/captured.json")
_assert_eq "verdict=authority_capture_suspected" \
  "$(echo "$out_b" | jq -r .verdict)" "authority_capture_suspected"

cw_b=$(echo "$out_b" | jq -r '.cw_por')
if awk -v v="$cw_b" -v t="0.3" 'BEGIN { exit (v > t) ? 0 : 1 }'; then
  _ok "cw_por=$cw_b > threshold 0.3"
else
  _fail "cw_por=$cw_b not greater than threshold 0.3"
fi

echo ""
echo "--- indeterminate: no ground_truth_match signal → verdict=indeterminate ---"
cat > "$TD/no_truth.json" <<'JSON'
{
  "voters": [
    {"voter_id":"glm",  "vote":"approve","confidence":0.85,"ground_truth_match":null},
    {"voter_id":"kimi", "vote":"approve","confidence":0.80,"ground_truth_match":null}
  ]
}
JSON
out_c=$(mo_compute_cw_por "$TD/no_truth.json")
_assert_eq "verdict=indeterminate" "$(echo "$out_c" | jq -r .verdict)" "indeterminate"
_assert_eq "cw_por=null"           "$(echo "$out_c" | jq -r .cw_por)"  "null"

echo ""
echo "--- error path: malformed verdict (missing .voters[]) → rc=2 ---"
echo '{"verdict":"approve"}' > "$TD/bad.json"
mo_compute_cw_por "$TD/bad.json" >/dev/null 2>&1
rc=$?
_assert_eq "malformed input → rc=2" "$rc" "2"

echo ""
echo "--- threshold tunable: MO_CW_POR_THRESHOLD=0.6 flips fixture B to healthy ---"
out_d=$(MO_CW_POR_THRESHOLD=0.6 mo_compute_cw_por "$TD/captured.json")
_assert_eq "high threshold → panel_healthy" \
  "$(echo "$out_d" | jq -r .verdict)" "panel_healthy"
_assert_eq "threshold echoed back" \
  "$(echo "$out_d" | jq -r .threshold)" "0.6"

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ]
