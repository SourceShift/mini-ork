#!/usr/bin/env bash
# tests/unit/test_adaptive_stability.sh — unit tests for lib/adaptive_stability.sh
# Usage: bash tests/unit/test_adaptive_stability.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
#
# Covers `mo_check_panel_stability` (Hu et al 2025, arxiv:2510.12697):
#   - HALT when round-over-round verdict_drift < threshold and current_round ≥ MIN_ROUNDS
#   - CONTINUE when current_round < MIN_ROUNDS (statistical floor)
#   - HALT unconditionally when current_round ≥ MAX_ROUNDS (compute ceiling)
#   - CONTINUE + reason=round_unencoded_default_continue when trace_ids
#     don't carry -r<N>- segments (fail-open)
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/adaptive_stability.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: adaptive_stability.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/adaptive_stability.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("""
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id           TEXT PRIMARY KEY,
  agent_version_id   TEXT,
  reviewer_verdict   TEXT,
  verifier_output    TEXT
);""")
con.commit(); con.close()
PY

# shellcheck source=/dev/null
source "$LIB"

# Helper — seed a 3-round panel where round 2→3 has zero drift
_seed_stable_panel() {
  sqlite3 "$MINI_ORK_DB" <<'SQL'
DELETE FROM execution_traces;
INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES
  ('tr-glm-r1-run-stable',  'glm',   'approve'),
  ('tr-kimi-r1-run-stable', 'kimi',  'reject'),
  ('tr-cdx-r1-run-stable',  'codex', 'reject'),
  ('tr-glm-r2-run-stable',  'glm',   'approve'),
  ('tr-kimi-r2-run-stable', 'kimi',  'approve'),
  ('tr-cdx-r2-run-stable',  'codex', 'reject'),
  ('tr-glm-r3-run-stable',  'glm',   'approve'),
  ('tr-kimi-r3-run-stable', 'kimi',  'approve'),
  ('tr-cdx-r3-run-stable',  'codex', 'reject');
SQL
}

echo ""
echo "--- happy path: 3 rounds, round 2→3 zero drift → HALT ---"
_seed_stable_panel
out_a=$(mo_check_panel_stability "run-stable" 3)
_assert_eq "recommendation=HALT"               "$(echo "$out_a" | jq -r .recommendation)" "HALT"
_assert_eq "reason=drift_below_threshold"      "$(echo "$out_a" | jq -r .reason)"          "drift_below_threshold"
_assert_eq "stable=true"                        "$(echo "$out_a" | jq -r .stable)"           "true"

echo ""
echo "--- statistical floor: round 1 below MIN_ROUNDS → CONTINUE ---"
out_b=$(mo_check_panel_stability "run-stable" 1)
_assert_eq "recommendation=CONTINUE"            "$(echo "$out_b" | jq -r .recommendation)" "CONTINUE"
_assert_eq "reason=below_min_rounds"            "$(echo "$out_b" | jq -r .reason)"          "below_min_rounds"

echo ""
echo "--- compute ceiling: MAX_ROUNDS reached → HALT regardless of drift ---"
out_c=$(MO_PANEL_MAX_ROUNDS=3 mo_check_panel_stability "run-stable" 3)
_assert_eq "recommendation=HALT"                "$(echo "$out_c" | jq -r .recommendation)" "HALT"
_assert_eq "reason=max_rounds_reached"          "$(echo "$out_c" | jq -r .reason)"          "max_rounds_reached"

echo ""
echo "--- fail-open: trace_ids without -r<N>- → CONTINUE + round_unencoded_default_continue ---"
sqlite3 "$MINI_ORK_DB" <<'SQL'
DELETE FROM execution_traces;
INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES
  ('tr-glm-run-noround', 'glm', 'approve');
SQL
out_d=$(mo_check_panel_stability "run-noround" 3)
_assert_eq "recommendation=CONTINUE"            "$(echo "$out_d" | jq -r .recommendation)" "CONTINUE"
_assert_eq "reason=round_unencoded_default_continue" \
  "$(echo "$out_d" | jq -r .reason)" "round_unencoded_default_continue"

echo ""
echo "--- threshold tunable: MO_PANEL_STABILITY_THRESHOLD=0.5 changes verdict at high-drift round ---"
# Same fixture as A, but at round 2 instead of 3 — round 1→2 drift = 1/3 ≈ 0.33
_seed_stable_panel
out_e=$(MO_PANEL_STABILITY_THRESHOLD=0.5 mo_check_panel_stability "run-stable" 2)
_assert_eq "drift 0.33 vs threshold 0.5 → HALT" \
  "$(echo "$out_e" | jq -r .recommendation)" "HALT"

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ]
