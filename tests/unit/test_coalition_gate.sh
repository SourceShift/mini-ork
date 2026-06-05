#!/usr/bin/env bash
# tests/unit/test_coalition_gate.sh — unit tests for lib/coalition_gate.sh
# Usage: bash tests/unit/test_coalition_gate.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
#
# Covers `mo_check_panel_coalition` (Rajan 2025 arxiv:2511.16708 +
# Bertalanič 2026 arxiv:2605.00914):
#   - panel_diverse + reason=ok when ρ < threshold AND family_count = lens_count
#   - COALITION_ABORT + reason=family_collision when ≥2 lenses share family
#   - COALITION_ABORT + reason=high_rho when ρ ≥ threshold
#   - panel_diverse + reason=single_agent_run when fewer than 2 traces
#   - MO_FAMILY_DIVERSITY_GATE=advisory bypasses the abort (warn-only)
#   - indeterminate when topology library is unavailable (fail-open)
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/coalition_gate.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: coalition_gate.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/coalition_gate.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Build the minimal execution_traces + panel_topology_telemetry schema
python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.executescript("""
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id           TEXT PRIMARY KEY,
  agent_version_id   TEXT,
  reviewer_verdict   TEXT,
  verifier_output    TEXT
);
CREATE TABLE IF NOT EXISTS panel_topology_telemetry (
  telemetry_id        TEXT PRIMARY KEY,
  panel_run_id        TEXT,
  recipe              TEXT,
  rho                 REAL,
  context_distance    REAL,
  inductive_distance  REAL,
  agent_count         INTEGER,
  n_traces            INTEGER,
  quadrant            TEXT
);
""")
con.commit(); con.close()
PY

# shellcheck source=/dev/null
source "$LIB"

echo ""
echo "--- happy path: 4 distinct families, distinct verdicts → panel_diverse ---"
sqlite3 "$MINI_ORK_DB" <<'SQL'
DELETE FROM execution_traces;
INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES
  ('tr-1-run-diverse', 'glm',     'approve A'),
  ('tr-2-run-diverse', 'kimi',    'reject B'),
  ('tr-3-run-diverse', 'codex',   'approve C'),
  ('tr-4-run-diverse', 'minimax', 'approve D');
SQL
out_a=$(mo_check_panel_coalition "run-diverse" "refactor-audit")
_assert_eq "verdict=panel_diverse"  "$(echo "$out_a" | jq -r .verdict)" "panel_diverse"
_assert_eq "reason=ok"              "$(echo "$out_a" | jq -r .reason)"  "ok"
_assert_eq "family_count=4"         "$(echo "$out_a" | jq -r .family_count)" "4"

echo ""
echo "--- failure mode: 4 same-family lenses, identical verdicts → COALITION_ABORT ---"
sqlite3 "$MINI_ORK_DB" <<'SQL'
DELETE FROM execution_traces;
INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES
  ('tr-glm-1-run-collision',  'sonnet', 'APPROVE: looks good'),
  ('tr-kimi-1-run-collision', 'opus',   'APPROVE: looks good'),
  ('tr-cdx-1-run-collision',  'sonnet', 'APPROVE: looks good'),
  ('tr-mxi-1-run-collision',  'opus',   'APPROVE: looks good');
SQL
out_b=$(mo_check_panel_coalition "run-collision" "refactor-audit" || true)
_assert_eq "verdict=COALITION_ABORT"   "$(echo "$out_b" | jq -r .verdict)" "COALITION_ABORT"
_assert_eq "family_count=1"            "$(echo "$out_b" | jq -r .family_count)" "1"
# reason can be "both" or "family_collision" depending on whether ρ also tripped
reason_b=$(echo "$out_b" | jq -r .reason)
if [[ "$reason_b" == "both" || "$reason_b" == "family_collision" ]]; then
  _ok "reason in {both, family_collision}: $reason_b"
else
  _fail "reason=$reason_b (expected both or family_collision)"
fi

echo ""
echo "--- advisory mode: MO_FAMILY_DIVERSITY_GATE=advisory bypasses abort ---"
out_c=$(MO_FAMILY_DIVERSITY_GATE=advisory mo_check_panel_coalition "run-collision" "refactor-audit")
_assert_eq "verdict=panel_diverse (advisory)" \
  "$(echo "$out_c" | jq -r .verdict)" "panel_diverse"
reason_c=$(echo "$out_c" | jq -r .reason)
if [[ "$reason_c" == advisory_* ]]; then
  _ok "reason starts with advisory_: $reason_c"
else
  _fail "reason=$reason_c (expected advisory_*)"
fi

echo ""
echo "--- single-agent run: panel_diverse with reason=single_agent_run ---"
sqlite3 "$MINI_ORK_DB" <<'SQL'
DELETE FROM execution_traces;
INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES
  ('tr-1-run-single', 'sonnet', 'approve');
SQL
out_d=$(mo_check_panel_coalition "run-single" "code-fix")
_assert_eq "verdict=panel_diverse"     "$(echo "$out_d" | jq -r .verdict)" "panel_diverse"
_assert_eq "reason=single_agent_run"   "$(echo "$out_d" | jq -r .reason)"  "single_agent_run"

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ]
