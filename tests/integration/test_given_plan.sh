#!/usr/bin/env bash
# tests/integration/test_given_plan.sh — MO_GIVEN_PLAN caller-supplied plan path.
#
# Contract (Python plan runtime, PR #72):
#   MO_GIVEN_PLAN=<path> supplies a complete plan JSON, so the planner LLM
#   dispatch is skipped — but the supplied plan flows through the SAME
#   extraction + schema/validation gates as an LLM-produced plan.
#   Fail-loud: an unreadable MO_GIVEN_PLAN is an error (exit 1), never a
#   silent fallback to the LLM.
#
# NOTE: the given-plan branch lives AFTER the --dry-run short-circuit, so these
# are REAL (non-dry) plan invocations. They still make no LLM call — that's the
# whole point — so they run credential-free in CI.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
plan_cmd=(env "PYTHONPATH=$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.cli.plan)
# The CI integration harness exports MINI_ORK_DRY_RUN=1 globally (to keep tests
# LLM-free). The given-plan branch lives AFTER the --dry-run short-circuit, so
# these invocations MUST run non-dry to reach it — and they still make no LLM
# call (MO_GIVEN_PLAN skips the planner dispatch), so this stays credential-free.
export MINI_ORK_DRY_RUN=0

TMPROOT=$(mktemp -d /tmp/ork-givenplan-XXXXXX)
trap 'cd /; rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT"
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
mini-ork init >/dev/null 2>&1

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

cat > "$TMPROOT/kickoff.md" <<'EOF'
# Fix bug in tally.js
## Problem
Off-by-one in computeTotal().
## Definition of Done
- npm test passes.
## Scope
- ONLY tally.js may be edited.
EOF

# A schema-valid plan (verifier_contract.checks non-empty; empty decomposition is
# valid — the D-008b node_type loop only runs over present steps). The unique
# marker in `objective` lets us prove the GIVEN plan was used verbatim, not an
# LLM re-derivation or a deterministic fallback.
MARKER="GIVEN_PLAN_MARKER_$$"
GIVEN="$TMPROOT/given-plan.json"
cat > "$GIVEN" <<JSON
{
  "objective": "${MARKER} :: fix off-by-one in tally.js",
  "assumptions": [],
  "decomposition": [],
  "dependencies": [],
  "risk_notes": [],
  "artifact_contract": { "outputs": [], "success_verifiers": [] },
  "verifier_contract": { "checks": [{ "id": "c1", "description": "npm test passes" }] }
}
JSON

echo "── integration: Python plan runtime MO_GIVEN_PLAN ──"

# ── (A) happy path: given plan used verbatim, planner LLM skipped ──
echo ""
echo "--- A. given plan is used + planner skipped ---"
export MINI_ORK_RUN_ID="run-givenplan-use-$$"
A_OUT_FILE="$TMPROOT/out-a.json"
A_RC=0
A_STDOUT=$(MO_GIVEN_PLAN="$GIVEN" "${plan_cmd[@]}" --out "$A_OUT_FILE" "$TMPROOT/kickoff.md" 2>"$TMPROOT/a.err") || A_RC=$?
A_STDERR=$(cat "$TMPROOT/a.err")

[ "$A_RC" -eq 0 ] && _ok "given-plan run exits 0" \
  || _fail "given-plan run expected exit 0, got $A_RC (stderr: $A_STDERR)"

echo "$A_STDERR" | grep -qiE 'using given plan|planner LLM skipped|MO_GIVEN_PLAN' \
  && _ok "stderr announces the given-plan path (planner skipped)" \
  || _fail "stderr did not announce given-plan path (got: $A_STDERR)"

if [ -f "$A_OUT_FILE" ] && grep -q "$MARKER" "$A_OUT_FILE"; then
  _ok "output plan carries the given-plan marker (given plan used verbatim, not re-derived)"
else
  _fail "output plan missing the given-plan marker (file: $A_OUT_FILE)"
fi

# The given plan still passed the schema gate (verifier_contract.checks present).
if [ -f "$A_OUT_FILE" ]; then
  HAS_VC=$(python3 -c "
import json,sys
p=json.load(open(sys.argv[1]))
print('ok' if p.get('verifier_contract',{}).get('checks') else 'missing')" "$A_OUT_FILE" 2>/dev/null || echo missing)
  [ "$HAS_VC" = "ok" ] \
    && _ok "given plan flowed through the schema gate (verifier_contract.checks present)" \
    || _fail "given plan output missing verifier_contract.checks (gate not applied?)"
fi

# Sanity: NO planner LLM dispatch happened (no plan-dispatch.err.log written for
# a skipped planner, and stderr must not claim an LLM planner was invoked).
echo "$A_STDERR" | grep -qi 'would invoke LLM planner\|invoking planner' \
  && _fail "stderr suggests the planner LLM was invoked despite MO_GIVEN_PLAN" \
  || _ok "no planner LLM invocation in the given-plan path"

# ── (B) fail-loud: unreadable MO_GIVEN_PLAN exits 1 (never silent LLM fallback) ──
echo ""
echo "--- B. unreadable MO_GIVEN_PLAN → exit 1 ---"
export MINI_ORK_RUN_ID="run-givenplan-bad-$$"
B_RC=0
B_STDERR=$(MO_GIVEN_PLAN="$TMPROOT/nope-does-not-exist-$$.json" \
  "${plan_cmd[@]}" --out "$TMPROOT/out-b.json" "$TMPROOT/kickoff.md" 2>&1 >/dev/null) || B_RC=$?

[ "$B_RC" -eq 1 ] && _ok "unreadable given-plan → exit 1" \
  || _fail "unreadable given-plan → expected exit 1, got $B_RC"

echo "$B_STDERR" | grep -qiE 'not readable|MO_GIVEN_PLAN' \
  && _ok "stderr explains the unreadable given-plan" \
  || _fail "stderr did not explain unreadable given-plan (got: $B_STDERR)"

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
