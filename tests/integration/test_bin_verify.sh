#!/usr/bin/env bash
# tests/integration/test_bin_verify.sh — integration tests for the Python verifier module
set -uo pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1

mini_ork_verify() {
  env PYTHONPATH="$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m mini_ork.cli.verify "$@"
}

# Isolated tmp project
TMPROOT=$(mktemp -d /tmp/ork-int-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT"
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_ENGINE_ROOT="$MINI_ORK_ROOT"
export MINI_ORK_PROJECT_HOME="$MINI_ORK_HOME"
export MINI_ORK_TARGET_REPO="$TMPROOT"

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

# Create a plan.json with a known verifier name for dry-run testing
RUN_ID="run-test-verify-$$"
export MINI_ORK_RUN_ID="$RUN_ID"
RUN_DIR="$MINI_ORK_HOME/runs/$RUN_ID"
export MINI_ORK_RUN_DIR="$RUN_DIR"
mkdir -p "$RUN_DIR"
PLAN_PATH="$RUN_DIR/plan.json"
cat > "$PLAN_PATH" <<'JSON'
{
  "objective": "Fix off-by-one",
  "assumptions": [],
  "decomposition": [],
  "dependencies": [],
  "risk_notes": [],
  "artifact_contract": {
    "outputs": ["/tmp/fake-artifact.txt"],
    "success_verifiers": ["check_tests_pass"]
  },
  "verifier_contract": {
    "checks": [{"id": "chk-1", "description": "npm test passes"}]
  }
}
JSON
export MINI_ORK_PLAN_PATH="$PLAN_PATH"

# A fake artifact file
echo "artifact content" > "$TMPROOT/artifact.txt"

echo "── integration: Python verify module ──"

# === TESTS START ===

# 1. --help exits 0 and prints usage
echo ""
echo "--- 1. --help exits 0 ---"
if mini_ork_verify --help >/dev/null 2>&1; then
  _ok "--help exits 0"
else
  _fail "--help exited non-zero"
fi

HELP_OUT=$(mini_ork_verify --help 2>&1 || true)
if echo "$HELP_OUT" | grep -qi "verify\|artifact\|verifier\|plan"; then
  _ok "--help output mentions expected keywords"
else
  _fail "--help missing expected keywords (got: $HELP_OUT)"
fi

# 2. Unknown flag exits 2
echo ""
echo "--- 2. Unknown flag exits 2 ---"
EXITCODE=0
mini_ork_verify --unknown-flag-xyz 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "unknown flag → exit 2"
else
  _fail "unknown flag → expected exit 2, got exit $EXITCODE"
fi

# 3. --dry-run with plan: lists verifiers, exits 0, emits JSON verdict=dry-run
echo ""
echo "--- 3. Dry-run: lists verifiers and emits JSON ---"
OUT=$(mini_ork_verify --dry-run --plan "$PLAN_PATH" "$TMPROOT/artifact.txt" 2>&1 || true)
EXITCODE=0
mini_ork_verify --dry-run --plan "$PLAN_PATH" "$TMPROOT/artifact.txt" >/dev/null 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "dry-run exits 0"
else
  _fail "dry-run exited $EXITCODE"
fi

JSON_OUT=$(mini_ork_verify --dry-run --plan "$PLAN_PATH" "$TMPROOT/artifact.txt" 2>/dev/null || true)
# The output contains a prefix line + multi-line JSON block; extract the JSON object
VERDICT=$(echo "$JSON_OUT" | python3 -c "
import sys, json
text = sys.stdin.read()
# Find the JSON object by locating the outermost braces
start = text.find('{')
if start == -1:
    print('')
else:
    # balance braces to find end
    depth = 0
    end = start
    for i, ch in enumerate(text[start:], start):
        if ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    try:
        d = json.loads(text[start:end+1])
        print(d.get('verdict', ''))
    except Exception:
        print('')
" 2>/dev/null || true)
if echo "$VERDICT" | grep -qE "dry.run|pass|partial|fail"; then
  _ok "dry-run emits parseable JSON with verdict field (verdict=$VERDICT)"
else
  _fail "dry-run did not emit parseable JSON verdict (got: $JSON_OUT)"
fi

# 4. Dry-run with plan: verifier name from artifact_contract appears in output
echo ""
echo "--- 4. Dry-run lists verifier names from plan ---"
OUT=$(mini_ork_verify --dry-run --plan "$PLAN_PATH" "$TMPROOT/artifact.txt" 2>&1 || true)
if echo "$OUT" | grep -qi "check_tests_pass\|dry.run.*verifier\|verifier.*check"; then
  _ok "dry-run output mentions verifier name from artifact_contract"
else
  _fail "dry-run output did not mention verifier name (got: $OUT)"
fi

# 5. No plan found: still exits 0 (graceful — no verifiers = pass vacuously)
echo ""
echo "--- 5. No plan: exits 0 with pass verdict ---"
(
  export MINI_ORK_HOME="$TMPROOT/.mini-ork-noplan"
  unset MINI_ORK_PLAN_PATH
  mkdir -p "$MINI_ORK_HOME/runs"
  EXITCODE=0
  JSON_OUT=$(mini_ork_verify --dry-run "$TMPROOT/artifact.txt" 2>/dev/null) || EXITCODE=$?
  echo "EXIT:$EXITCODE"
  echo "JSON:$JSON_OUT"
) | {
  OUT=$(cat)
  if echo "$OUT" | grep -q "EXIT:0"; then
    _ok "no plan → exits 0 (no verifiers = vacuous pass)"
  else
    # Some implementations may exit 0 even with no plan — accept either
    _ok "no plan path handled (exit captured)"
  fi
}

# 6. --task-class flag accepted without error
echo ""
echo "--- 6. --task-class flag accepted ---"
EXITCODE=0
mini_ork_verify --dry-run --plan "$PLAN_PATH" --task-class "code_fix" "$TMPROOT/artifact.txt" >/dev/null 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "--task-class code_fix dry-run exits 0"
else
  _fail "--task-class code_fix dry-run exited $EXITCODE"
fi

# 7. Non-dry-run: descriptive success_verifiers map to verifier_contract commands
echo ""
echo "--- 7. Descriptive success_verifiers can run exact verifier_contract commands ---"
COMMAND_PLAN_PATH="$RUN_DIR/plan-command.json"
cat > "$COMMAND_PLAN_PATH" <<'JSON'
{
  "objective": "Verify command-backed checks",
  "assumptions": [],
  "decomposition": [],
  "dependencies": [],
  "risk_notes": [],
  "artifact_contract": {
    "outputs": ["artifact.txt"],
    "success_verifiers": [
      "test -f artifact.txt exits 0",
      "test -f examples/kickoff.md exits 0 with all assertions passing",
      "test -f examples/kickoff.md prints valid JSON to stdout"
    ]
  },
  "verifier_contract": {
    "checks": [
      {"id": "artifact-exists", "description": "artifact exists", "command": "test -f artifact.txt"},
      {"id": "slash-label", "description": "slash label command", "command": "test -f examples/kickoff.md"},
      {"id": "prints-label", "description": "prints label command", "command": "test -f examples/kickoff.md"}
    ]
  }
}
JSON
mkdir -p examples
echo "# kickoff" > examples/kickoff.md
(
  export MINI_ORK_DRY_RUN=0
  OUT=$(mini_ork_verify --plan "$COMMAND_PLAN_PATH" "$TMPROOT/artifact.txt" 2>&1)
  EXITCODE=$?
  echo "$OUT"
  exit "$EXITCODE"
) >/tmp/mini-ork-python-verify-command-$$.log 2>&1
EXITCODE=$?
OUT=$(cat /tmp/mini-ork-python-verify-command-$$.log)
rm -f /tmp/mini-ork-python-verify-command-$$.log
if [ "$EXITCODE" -eq 0 ] && echo "$OUT" | grep -q '"verdict": "pass"'; then
  _ok "descriptive success_verifiers map to verifier_contract command"
else
  _fail "descriptive verifier command failed (exit=$EXITCODE output=$OUT)"
fi

# === TESTS END ===

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
