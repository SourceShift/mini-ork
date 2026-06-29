#!/usr/bin/env bash
# tests/integration/test_gate_grounded_rejection.sh
#
# Phase 2 integration test for HarnessBridge Technique 4 (grounded rejection).
# Drives each of the 5 oracle gates into its hard-block path and asserts that
# exactly one (concern, evidence, suggestion) row lands in grounded_rejections
# with the right gate_name + verdict. Also drives a pass / indeterminate path
# per gate and asserts ZERO rows — the emit must fire only on hard-block.
#
# Gates under test:
#   coalition_gate.sh           COALITION_ABORT     -> fail
#   krippendorff_alpha_gate.sh  ALPHA_ESCALATE      -> needs_revision
#   citation_verifier_mechanical.sh CITATION_UNDERCOVERED -> fail
#   refute_or_promote_gate.sh   REFUTE_FAILED       -> fail
#   honest_ci_gate.sh           CI_TOO_WIDE         -> needs_revision

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib"

_td=$(mktemp -d)
trap 'rm -rf "$_td"' EXIT

export MINI_ORK_DB="$_td/state.db"

# --- migrate: grounded_rejections (output) + coalition input schema ----------
sqlite3 "$MINI_ORK_DB" < "$MINI_ORK_ROOT/db/migrations/0037_grounded_rejection.sql"
sqlite3 "$MINI_ORK_DB" <<'SQL'
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
SQL

# Source all gates (defines mo_check_* + sources gates_common -> mo_grounded_rejection).
# The self-test guard `[ BASH_SOURCE == 0 ]` is false here, so no fixtures run.
# shellcheck source=/dev/null
source "$LIB/gates_common.sh"
# shellcheck source=/dev/null
source "$LIB/coalition_gate.sh"
# shellcheck source=/dev/null
source "$LIB/krippendorff_alpha_gate.sh"
# shellcheck source=/dev/null
source "$LIB/citation_verifier_mechanical.sh"
# shellcheck source=/dev/null
source "$LIB/refute_or_promote_gate.sh"
# shellcheck source=/dev/null
source "$LIB/honest_ci_gate.sh"

if ! declare -f mo_grounded_rejection >/dev/null 2>&1; then
  echo "FATAL: mo_grounded_rejection not defined after sourcing gates" >&2
  exit 1
fi

PASS=0
FAIL=0

_count_rows() { sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM grounded_rejections" 2>/dev/null || echo "ERR"; }

# assert_block <label> <expected_gate_name> <expected_verdict>
# Caller has already run the gate; we assert exactly one NEW row with the
# right gate_name/verdict and non-empty tuple fields. Uses globals _before/_after.
assert_block() {
  local label="$1" want_gate="$2" want_verdict="$3"
  local delta=$(( _after - _before ))
  if [ "$delta" -ne 1 ]; then
    echo "FAIL  $label: expected exactly 1 new row, got $delta" >&2
    FAIL=$((FAIL+1)); return
  fi
  # Inspect the most-recent row.
  local row
  row=$(sqlite3 -separator '|' "$MINI_ORK_DB" \
    "SELECT gate_name, verdict, concern, evidence_summary, suggestion
       FROM grounded_rejections ORDER BY rowid DESC LIMIT 1" 2>/dev/null)
  IFS='|' read -r g v concern evidence suggestion <<< "$row"
  if [ "$g" != "$want_gate" ]; then
    echo "FAIL  $label: gate_name='$g' want '$want_gate'" >&2; FAIL=$((FAIL+1)); return
  fi
  if [ "$v" != "$want_verdict" ]; then
    echo "FAIL  $label: verdict='$v' want '$want_verdict'" >&2; FAIL=$((FAIL+1)); return
  fi
  if [ -z "$concern" ] || [ -z "$evidence" ] || [ -z "$suggestion" ]; then
    echo "FAIL  $label: empty tuple field (concern='$concern' evidence='$evidence' suggestion='$suggestion')" >&2
    FAIL=$((FAIL+1)); return
  fi
  echo "PASS  $label: 1 row gate=$g verdict=$v tuple=(non-empty x3)"
  PASS=$((PASS+1))
}

# assert_no_row <label>: caller ran a pass/indeterminate path; assert 0 new rows.
assert_no_row() {
  local label="$1"
  local delta=$(( _after - _before ))
  if [ "$delta" -ne 0 ]; then
    echo "FAIL  $label: expected 0 new rows on pass/indeterminate, got $delta" >&2
    FAIL=$((FAIL+1)); return
  fi
  echo "PASS  $label: 0 rows (no emit on pass/indeterminate)"
  PASS=$((PASS+1))
}

echo "==================================================================="
echo " Phase 2 grounded-rejection wiring — 5 gates x (block + pass) paths"
echo " DB: $MINI_ORK_DB"
echo "==================================================================="

# ============================ 1. COALITION =================================
# Block: 4 single-family (anthropic) lenses -> COALITION_ABORT.
sqlite3 "$MINI_ORK_DB" <<'SQL'
DELETE FROM execution_traces;
INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES
  ('tr-glm-1-run-fixture-12345',  'sonnet', 'APPROVE: looks good'),
  ('tr-kimi-1-run-fixture-12345', 'opus',   'APPROVE: looks good'),
  ('tr-cdx-1-run-fixture-12345',  'sonnet', 'APPROVE: looks good'),
  ('tr-mxi-1-run-fixture-12345',  'opus',   'APPROVE: looks good');
SQL
_before=$(_count_rows)
mo_check_panel_coalition "run-fixture-12345" "refactor-audit" >/dev/null 2>&1
_after=$(_count_rows)
assert_block "coalition/block" "coalition" "fail"

# Pass: 4 distinct families -> panel_diverse.
sqlite3 "$MINI_ORK_DB" <<'SQL'
DELETE FROM execution_traces;
INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES
  ('tr-1-run-fixture-diverse', 'glm',     'APPROVE A'),
  ('tr-2-run-fixture-diverse', 'kimi',    'REJECT B'),
  ('tr-3-run-fixture-diverse', 'codex',   'APPROVE C'),
  ('tr-4-run-fixture-diverse', 'minimax', 'APPROVE D');
SQL
_before=$(_count_rows)
mo_check_panel_coalition "run-fixture-diverse" "refactor-audit" >/dev/null 2>&1
_after=$(_count_rows)
assert_no_row "coalition/pass"

# ======================= 2. KRIPPENDORFF ALPHA =============================
export MINI_ORK_RUN_ID="run-itest-krip"
_kdir="$_td/krip"; mkdir -p "$_kdir"
# Block: low agreement -> ALPHA_ESCALATE.
cat > "$_kdir/panel-verdict.json" <<'JSON'
{ "lens_scores": {
  "glm":     [1, 9, 2, 8, 3],
  "kimi":    [9, 1, 8, 2, 9],
  "codex":   [2, 8, 3, 7, 2],
  "minimax": [8, 2, 9, 3, 8]
} }
JSON
_before=$(_count_rows)
mo_check_panel_alpha "$_kdir" >/dev/null 2>&1
_after=$(_count_rows)
assert_block "krippendorff/block" "krippendorff_alpha" "needs_revision"

# Pass: high agreement -> panel_calibrated.
cat > "$_kdir/panel-verdict.json" <<'JSON'
{ "lens_scores": {
  "glm":     [8, 7, 9, 8, 7],
  "kimi":    [8, 7, 9, 8, 7],
  "codex":   [8, 7, 9, 8, 7],
  "minimax": [8, 7, 9, 8, 7]
} }
JSON
_before=$(_count_rows)
mo_check_panel_alpha "$_kdir" >/dev/null 2>&1
_after=$(_count_rows)
assert_no_row "krippendorff/pass"

# ======================= 3. CITATION VERIFIER =============================
_crepo="$_td/crepo"; mkdir -p "$_crepo/src" "$_crepo/docs"
printf 'line1\nline2\nline3\nline4\nline5\n' > "$_crepo/src/foo.ts"
printf 'a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n'       > "$_crepo/src/bar.py"
# Block: half citations invalid -> CITATION_UNDERCOVERED.
cat > "$_crepo/docs/synthesis.md" <<'MD'
Mixed: real src/foo.ts:2 and ghost src/missing.ts:5 and
out-of-bounds src/foo.ts:9999 and real src/bar.py:1.
MD
_before=$(_count_rows)
MINI_ORK_ROOT="$_crepo" mo_check_citations "$_crepo/docs/synthesis.md" "$_td" >/dev/null 2>&1
_after=$(_count_rows)
assert_block "citation/block" "citation_verifier" "fail"

# Pass: all citations valid -> citations_covered.
cat > "$_crepo/docs/synthesis.md" <<'MD'
Three valid anchors. See src/foo.ts:2 for the first claim and
src/foo.ts:3-4 for the second, and src/bar.py:7 for the third.
MD
_before=$(_count_rows)
MINI_ORK_ROOT="$_crepo" mo_check_citations "$_crepo/docs/synthesis.md" "$_td" >/dev/null 2>&1
_after=$(_count_rows)
assert_no_row "citation/pass"

# ======================= 4. REFUTE OR PROMOTE ============================
_rdir="$_td/refute"; mkdir -p "$_rdir"
mo_generate_fabrications 5 "$_rdir/fab.json" >/dev/null 2>&1
# Block: validator cites 3 of 5 fabrications -> REFUTE_FAILED.
python3 -c "
import json
fabs = json.load(open('$_rdir/fab.json'))
with open('$_rdir/findings-bad.md', 'w') as f:
    f.write('## Findings\n\n')
    for x in fabs[:3]:
        f.write(f'- {x[\"path\"]}:{x[\"line\"]} — {x[\"claim\"]}\n')
    f.write('- src/legitimate/foo.ts:10 — real finding\n')
"
_before=$(_count_rows)
mo_check_fabrication_survival "$_rdir/findings-bad.md" "$_rdir/fab.json" "$_rdir" >/dev/null 2>&1
_after=$(_count_rows)
assert_block "refute/block" "refute_or_promote" "fail"

# Pass: clean validator (no fabrications cited) -> validator_grounded.
cat > "$_rdir/findings-clean.md" <<'MD'
## Findings

1. Real issue in src/auth/login.ts:42 — token expiration not validated.
2. Race condition in src/db/pool.ts:118 — connection reuse before reset.
3. Missing null check in src/api/handler.ts:7.
MD
_before=$(_count_rows)
mo_check_fabrication_survival "$_rdir/findings-clean.md" "$_rdir/fab.json" "$_rdir" >/dev/null 2>&1
_after=$(_count_rows)
assert_no_row "refute/pass"

# ======================= 5. HONEST CI ===================================
_hdir="$_td/honest"; mkdir -p "$_hdir"
# Block: split panel -> wide CIs -> CI_TOO_WIDE.
cat > "$_hdir/findings.json" <<'JSON'
{ "findings": [
  {"id": "F-001", "title": "Race condition",
   "lens_votes": {"glm": 0, "kimi": 3, "codex": 0, "minimax": 3}},
  {"id": "F-002", "title": "Stale cache read",
   "lens_votes": {"glm": 1, "kimi": 4, "codex": 0, "minimax": 5}},
  {"id": "F-003", "title": "Missing escape",
   "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}}
] }
JSON
_before=$(_count_rows)
mo_check_ci_widths "$_hdir/findings.json" "$_hdir" >/dev/null 2>&1
_after=$(_count_rows)
assert_block "honest_ci/block" "honest_ci" "needs_revision"

# Pass: tight panel -> ci_within_band.
cat > "$_hdir/findings.json" <<'JSON'
{ "findings": [
  {"id": "F-001", "title": "Auth retry storm",
   "lens_votes": {"glm": 2, "kimi": 2, "codex": 2, "minimax": 2}},
  {"id": "F-002", "title": "Cache key collision",
   "lens_votes": {"glm": 3, "kimi": 3, "codex": 3, "minimax": 3}},
  {"id": "F-003", "title": "Null cursor crash",
   "lens_votes": {"glm": 1, "kimi": 1, "codex": 1, "minimax": 1}}
] }
JSON
_before=$(_count_rows)
mo_check_ci_widths "$_hdir/findings.json" "$_hdir" >/dev/null 2>&1
_after=$(_count_rows)
assert_no_row "honest_ci/pass"

# ========================== TOTALS ======================================
echo "==================================================================="
echo " total rows in grounded_rejections: $(_count_rows) (expect 5)"
echo " PASS=$PASS  FAIL=$FAIL"
echo "==================================================================="
[ "$FAIL" -eq 0 ] || { echo "INTEGRATION TEST FAILED" >&2; exit 1; }
echo "all grounded-rejection integration assertions passed."
