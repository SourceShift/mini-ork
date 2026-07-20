#!/usr/bin/env bash
# tests/integration/test_bin_plan.sh — Python plan entrypoint integration tests
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
plan_cmd=(env "PYTHONPATH=$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.ported.mini_ork_plan)
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

echo "── integration: Python plan runtime ──"

# === TESTS START ===

# 1. --help exits 0 and prints usage
echo ""
echo "--- 1. --help exits 0 ---"
if "${plan_cmd[@]}" --help >/dev/null 2>&1; then
  _ok "--help exits 0"
else
  _fail "--help exited non-zero"
fi

HELP_OUT=$("${plan_cmd[@]}" --help 2>&1 || true)
if echo "$HELP_OUT" | grep -qi "plan\|kickoff\|usage\|verifier"; then
  _ok "--help output mentions expected keywords"
else
  _fail "--help output missing expected keywords (got: $HELP_OUT)"
fi

# 2. Missing required arg exits 2
echo ""
echo "--- 2. Missing required arg exits 2 ---"
EXITCODE=0
"${plan_cmd[@]}" 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "no args → exit 2"
else
  _fail "no args → expected exit 2, got exit $EXITCODE"
fi

# 3. Nonexistent kickoff file exits 2
echo ""
echo "--- 3. Nonexistent file exits 2 ---"
EXITCODE=0
"${plan_cmd[@]}" /tmp/does-not-exist-ork-$$$.md 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "nonexistent kickoff → exit 2"
else
  _fail "nonexistent kickoff → expected exit 2, got exit $EXITCODE"
fi

# 4. Happy path (--dry-run): writes placeholder plan.json and emits plan_path=
echo ""
echo "--- 4. Dry-run: writes plan.json + emits plan_path= ---"
export MINI_ORK_RUN_ID="run-test-plan-$$"
OUT=$("${plan_cmd[@]}" --dry-run "$TMPROOT/kickoff.md" 2>/dev/null || true)
STDERR_OUT=$("${plan_cmd[@]}" --dry-run "$TMPROOT/kickoff.md" 2>&1 >/dev/null || true)

if echo "$OUT" | grep -qE '^plan_path='; then
  PLAN_PATH=$(echo "$OUT" | grep -E '^plan_path=' | head -1 | cut -d= -f2)
  _ok "plan_path=${PLAN_PATH} emitted"
else
  _fail "stdout did not contain plan_path= line (got: $OUT)"
fi

if echo "$STDERR_OUT" | grep -qi "dry.run\|dry_run"; then
  _ok "stderr mentions dry-run mode"
else
  _fail "stderr did not mention dry-run (got: $STDERR_OUT)"
fi

# 5. plan.json written by dry-run is valid JSON with verifier_contract
echo ""
echo "--- 5. Dry-run plan.json is valid JSON with verifier_contract ---"
PLAN_PATH=$("${plan_cmd[@]}" --dry-run "$TMPROOT/kickoff.md" 2>/dev/null \
  | grep -E '^plan_path=' | head -1 | cut -d= -f2 || true)
if [ -n "$PLAN_PATH" ] && [ -f "$PLAN_PATH" ]; then
  VALID_JSON=$(python3 -c "import json,sys; json.load(open(sys.argv[1])); print('ok')" "$PLAN_PATH" 2>/dev/null || echo "bad")
  if [ "$VALID_JSON" = "ok" ]; then
    _ok "plan.json is valid JSON"
  else
    _fail "plan.json is NOT valid JSON"
  fi

  HAS_VC=$(python3 -c "
import json, sys
p = json.load(open(sys.argv[1]))
checks = p.get('verifier_contract', {}).get('checks', [])
print('ok' if checks else 'missing')
" "$PLAN_PATH" 2>/dev/null || echo "missing")
  if [ "$HAS_VC" = "ok" ]; then
    _ok "plan.json contains verifier_contract.checks"
  else
    _fail "plan.json missing verifier_contract.checks"
  fi
else
  _fail "plan.json path missing or file not created"
fi

# 6. --task-class override reflected in plan output
echo ""
echo "--- 6. --task-class override accepted ---"
export MINI_ORK_RUN_ID="run-test-plan-tc-$$"
OUT=$("${plan_cmd[@]}" --dry-run --task-class "test_class" "$TMPROOT/kickoff.md" 2>/dev/null || true)
if echo "$OUT" | grep -qE 'task_class=test_class'; then
  _ok "--task-class test_class reflected in stdout"
else
  _fail "--task-class test_class not in stdout (got: $OUT)"
fi

# 7. --out flag: plan written to specified path
echo ""
echo "--- 7. --out flag: writes plan to given path ---"
CUSTOM_OUT="$TMPROOT/custom-plan.json"
export MINI_ORK_RUN_ID="run-test-plan-out-$$"
"${plan_cmd[@]}" --dry-run --out "$CUSTOM_OUT" "$TMPROOT/kickoff.md" >/dev/null 2>&1 || true
if [ -f "$CUSTOM_OUT" ]; then
  _ok "--out $CUSTOM_OUT: file written"
else
  _fail "--out $CUSTOM_OUT: file NOT written"
fi

# === TESTS END ===

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
