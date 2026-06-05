#!/usr/bin/env bash
# tests/unit/test_promotion_synthesis_gate.sh — unit tests for
# `mo_promote_synthesis_gate` (W1-D function added to lib/promotion_gate.sh).
# Usage: bash tests/unit/test_promotion_synthesis_gate.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
#
# Covers the selective-feedback conjunction gate for synthesis-class
# task classes (Adapala 2025 arxiv:2509.10509 + Zenil 2026
# arxiv:2601.05280):
#   - Deterministic classes (code_fix, db_migration) bypass with
#     reason=deterministic_class — no panel inspection
#   - Synthesis class all-pass (panel_score ≥ 80 + cw_por_status=passed
#     + ≥ 1 structural signal) → approved
#   - Synthesis class low panel_score → rejected with reason=low_panel_score
#   - Synthesis class missing structural signal → rejected with
#     reason=no_structural_signal
#   - Soft-dep on cw_por.sh: gate default-passes that check when the
#     library is absent or returns indeterminate
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/promotion_gate.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: promotion_gate.sh::mo_promote_synthesis_gate ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/promotion_gate.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TD=$(mktemp -d)
trap 'rm -rf "$TD"' EXIT

# shellcheck source=/dev/null
source "$LIB"

if ! declare -f mo_promote_synthesis_gate > /dev/null; then
  _skip "mo_promote_synthesis_gate function not exported — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

echo ""
echo "--- deterministic class bypass: code_fix → approved/deterministic_class ---"
echo '{"panel_score":0,"voters":[],"structural":{}}' > "$TD/det.json"
out_a=$(mo_promote_synthesis_gate "$TD/det.json" code_fix)
rc_a=$?
_assert_eq "code_fix → rc=0"           "$rc_a"                                  "0"
_assert_eq "decision=approved"         "$(echo "$out_a" | jq -r .decision)"    "approved"
_assert_eq "reason=deterministic_class" "$(echo "$out_a" | jq -r .reason)"     "deterministic_class"

echo ""
echo "--- synthesis class all-pass: panel_score=87.5 + healthy CW-POR + structural ---"
cat > "$TD/healthy.json" <<'JSON'
{
  "panel_score": 87.5,
  "voters": [
    {"voter_id":"glm",    "vote":"approve","confidence":0.85,"ground_truth_match":true},
    {"voter_id":"kimi",   "vote":"approve","confidence":0.80,"ground_truth_match":true},
    {"voter_id":"codex",  "vote":"approve","confidence":0.75,"ground_truth_match":true}
  ],
  "structural": {
    "citation_density_per_lens": 5.2,
    "file_coverage_delta": 3,
    "finding_cardinality": 11
  }
}
JSON
out_b=$(mo_promote_synthesis_gate "$TD/healthy.json" research_synthesis)
rc_b=$?
_assert_eq "all-pass → rc=0"           "$rc_b"                                  "0"
_assert_eq "decision=approved"          "$(echo "$out_b" | jq -r .decision)"    "approved"
_assert_eq "reason=all_conditions_met"  "$(echo "$out_b" | jq -r .reason)"      "all_conditions_met"

echo ""
echo "--- low panel_score: 62 < 80 threshold → rejected/low_panel_score ---"
cat > "$TD/low_score.json" <<'JSON'
{
  "panel_score": 62.0,
  "voters": [],
  "structural": {
    "citation_density_per_lens": 8.0,
    "file_coverage_delta": 5,
    "finding_cardinality": 20
  }
}
JSON
out_c=$(mo_promote_synthesis_gate "$TD/low_score.json" refactor_audit)
rc_c=$?
_assert_eq "low_score → rc=1"          "$rc_c"                                  "1"
_assert_eq "decision=rejected"          "$(echo "$out_c" | jq -r .decision)"    "rejected"
_assert_eq "reason=low_panel_score"     "$(echo "$out_c" | jq -r .reason)"      "low_panel_score"

echo ""
echo "--- no structural signal: panel=95 but all 3 structural thresholds missed ---"
cat > "$TD/no_signal.json" <<'JSON'
{
  "panel_score": 95.0,
  "voters": [],
  "structural": {
    "citation_density_per_lens": 1.0,
    "file_coverage_delta": 0,
    "finding_cardinality": 2
  }
}
JSON
out_d=$(mo_promote_synthesis_gate "$TD/no_signal.json" blog_post)
rc_d=$?
_assert_eq "no_signal → rc=1"          "$rc_d"                                  "1"
_assert_eq "decision=rejected"         "$(echo "$out_d" | jq -r .decision)"     "rejected"
_assert_eq "reason=no_structural_signal" "$(echo "$out_d" | jq -r .reason)"     "no_structural_signal"

echo ""
echo "--- threshold tunable: MO_PROMOTE_SCORE_THRESHOLD=60 lets fixture C pass score gate ---"
out_e=$(MO_PROMOTE_SCORE_THRESHOLD=60 mo_promote_synthesis_gate "$TD/low_score.json" refactor_audit)
rc_e=$?
_assert_eq "low_threshold → rc=0"      "$rc_e"                                  "0"
_assert_eq "decision=approved"          "$(echo "$out_e" | jq -r .decision)"    "approved"

echo ""
echo "--- malformed input: missing .panel_score → rc=2 ---"
echo '{"voters":[]}' > "$TD/bad.json"
mo_promote_synthesis_gate "$TD/bad.json" research_synthesis >/dev/null 2>&1
_assert_eq "malformed → rc=2"          "$?"                                     "2"

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ]
