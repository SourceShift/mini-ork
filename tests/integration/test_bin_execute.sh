#!/usr/bin/env bash
# tests/integration/test_bin_execute.sh — integration tests for the native execute route
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
unset MINI_ORK_ENGINE_ROOT MINI_ORK_PROJECT_HOME MINI_ORK_TARGET_REPO
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1

# Isolated tmp project
TMPROOT=$(mktemp -d /tmp/ork-int-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT"
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

# Bootstrap
mini-ork init >/dev/null 2>&1

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

# Synthetic kickoff
cat > "$TMPROOT/kickoff.md" <<'EOF'
# Fix bug in tally.js
## Problem
Off-by-one in computeTotal().
## Definition of Done
- npm test passes.
## Scope
- ONLY tally.js may be edited.
EOF

# Create a minimal valid plan.json for execute to consume
RUN_ID="run-test-execute-$$"
export MINI_ORK_RUN_ID="$RUN_ID"
RUN_DIR="$MINI_ORK_HOME/runs/$RUN_ID"
mkdir -p "$RUN_DIR"
PLAN_PATH="$RUN_DIR/plan.json"
cat > "$PLAN_PATH" <<'JSON'
{
  "objective": "Fix off-by-one in computeTotal",
  "assumptions": ["tests are runnable"],
  "decomposition": [
    {"id": "step-1", "description": "Implement the fix", "node_type": "implementer", "depends_on": []}
  ],
  "dependencies": [],
  "risk_notes": [],
  "artifact_contract": { "outputs": [], "success_verifiers": [] },
  "verifier_contract": { "checks": [{"id": "chk-1", "description": "npm test passes"}] }
}
JSON
export MINI_ORK_PLAN_PATH="$PLAN_PATH"

echo "── integration: mini-ork execute ──"

# === TESTS START ===

# 1. --help exits 0 and prints usage
echo ""
echo "--- 1. --help exits 0 ---"
if mini-ork execute --help >/dev/null 2>&1; then
  _ok "--help exits 0"
else
  _fail "--help exited non-zero"
fi

HELP_OUT=$(mini-ork execute --help 2>&1 || true)
if echo "$HELP_OUT" | grep -qi "execute\|plan\|node.type\|dispatch"; then
  _ok "--help output mentions expected keywords"
else
  _fail "--help output missing expected keywords (got: $HELP_OUT)"
fi

# 2. Missing plan exits 2 (no plan.json and env unset)
echo ""
echo "--- 2. No plan found exits 2 ---"
TMPROOT2=$(mktemp -d /tmp/ork-int-test-noPlan-XXXXXX)
trap 'rm -rf "$TMPROOT2"' EXIT
(
  export MINI_ORK_HOME="$TMPROOT2/.mini-ork"
  export MINI_ORK_DB="$TMPROOT2/.mini-ork/state.db"
  unset MINI_ORK_PLAN_PATH MINI_ORK_RUN_ID MINI_ORK_RUN_DIR MINI_ORK_RECIPE \
    MINI_ORK_WORKFLOW MINI_ORK_RECOVERY_CLOSURE MINI_ORK_RECOVERY_FROM
  mkdir -p "$TMPROOT2/.mini-ork/runs"
  EXITCODE=0
  mini-ork execute 2>/dev/null || EXITCODE=$?
  echo "EXIT:$EXITCODE"
) | grep -q "EXIT:2" && _ok "no plan found → exit 2" || _fail "no plan → expected exit 2"

# 3. Nonexistent explicit plan path exits 2
# Unset env so positional arg is the only source; pass a nonexistent path.
echo ""
echo "--- 3. Nonexistent plan path exits 2 ---"
EXITCODE=0
(
  unset MINI_ORK_PLAN_PATH
  mini-ork execute /tmp/no-such-plan-$$$.json 2>/dev/null
  echo "$?"
) | grep -q "^2$" && _ok "nonexistent plan path → exit 2" || _fail "nonexistent plan path → expected exit 2"

# 4. Happy path (dry-run): reads plan via env, prints dispatch plan, exits 0
# Note: MINI_ORK_PLAN_PATH is already exported — do NOT pass plan as positional arg
# (execute's arg parser treats it as "Unexpected argument" when env is already set).
echo ""
echo "--- 4. Dry-run: reads plan via env + prints dispatch lines ---"
OUT=$(mini-ork execute --dry-run 2>&1 || true)
if echo "$OUT" | grep -qi "dry.run\|would dispatch\|execute"; then
  _ok "dry-run output mentions dispatch/dry-run"
else
  _fail "dry-run output missing expected markers (got: $OUT)"
fi

EXITCODE=0
mini-ork execute --dry-run >/dev/null 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "dry-run exits 0"
else
  _fail "dry-run exited $EXITCODE (expected 0)"
fi

# 5. --node-type filter: only dispatches matching node types (plan via env)
echo ""
echo "--- 5. --node-type filter accepted ---"
EXITCODE=0
mini-ork execute --dry-run --node-type "implementer" >/dev/null 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "--node-type implementer dry-run exits 0"
else
  _fail "--node-type implementer dry-run exited $EXITCODE"
fi

# 6. --dispatch-mode override accepted (plan via env)
echo ""
echo "--- 6. --dispatch-mode override accepted ---"
EXITCODE=0
mini-ork execute --dry-run --dispatch-mode "parallel" >/dev/null 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "--dispatch-mode parallel dry-run exits 0"
else
  _fail "--dispatch-mode parallel dry-run exited $EXITCODE"
fi

# 7. Unknown flag exits 2
echo ""
echo "--- 7. Unknown flag exits 2 ---"
EXITCODE=0
mini-ork execute --unknown-flag-xyz 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "unknown flag → exit 2"
else
  _fail "unknown flag → expected exit 2, got exit $EXITCODE"
fi

# 8. The public CLI must own execute in-process, outside sibling Bash routing.
echo ""
echo "--- 8. execute is an in-process native route ---"
if PYTHONPATH="$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
from mini_ork.cli import main as mini_ork_cli
assert "execute" not in mini_ork_cli._EXEC_SUBS
PY
then
  _ok "execute bypasses sibling-entrypoint subprocess routing"
else
  _fail "execute remains in sibling-entrypoint subprocess routing"
fi

# === TESTS END ===

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
