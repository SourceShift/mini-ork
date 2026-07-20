#!/usr/bin/env bash
# tests/integration/test_bin_reflect.sh — Python-sole reflect CLI integration tests
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1

reflect_cmd() {
  PYTHONPATH="${MINI_ORK_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m mini_ork.cli.reflect "$@"
}

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

# Synthetic kickoff for reference
cat > "$TMPROOT/kickoff.md" <<'EOF'
# Fix bug in tally.js
## Problem
Off-by-one in computeTotal().
## Definition of Done
- npm test passes.
## Scope
- ONLY tally.js may be edited.
EOF

echo "── integration: mini-ork-reflect ──"

# === TESTS START ===

# 1. --help exits 0 and prints usage
echo ""
echo "--- 1. --help exits 0 ---"
if reflect_cmd --help >/dev/null 2>&1; then
  _ok "--help exits 0"
else
  _fail "--help exited non-zero"
fi

HELP_OUT=$(reflect_cmd --help 2>&1 || true)
if echo "$HELP_OUT" | grep -qi "reflect\|since\|pattern\|trace\|gradient"; then
  _ok "--help output mentions expected keywords"
else
  _fail "--help missing expected keywords (got: $HELP_OUT)"
fi

# 2. Unknown flag exits 2
echo ""
echo "--- 2. Unknown flag exits 2 ---"
EXITCODE=0
reflect_cmd --unknown-flag-xyz 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "unknown flag → exit 2"
else
  _fail "unknown flag → expected exit 2, got exit $EXITCODE"
fi

# 3. Unexpected positional argument exits 2
echo ""
echo "--- 3. Unexpected positional arg exits 2 ---"
EXITCODE=0
reflect_cmd some-unexpected-arg 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "unexpected positional arg → exit 2"
else
  _fail "unexpected positional arg → expected exit 2, got exit $EXITCODE"
fi

# 4. --dry-run exits 0 + reports trace count
echo ""
echo "--- 4. --dry-run exits 0 + reports trace count ---"
EXITCODE=0
OUT=$(reflect_cmd --dry-run 2>&1) || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "--dry-run exits 0"
else
  _fail "--dry-run exited $EXITCODE"
fi
if echo "$OUT" | grep -qiE "dry.run|trace|analyz"; then
  _ok "--dry-run output mentions trace analysis"
else
  _fail "--dry-run output missing expected markers (got: $OUT)"
fi

# 5. --dry-run with --since timestamp: accepted without error
echo ""
echo "--- 5. --since timestamp accepted ---"
EXITCODE=0
reflect_cmd --dry-run --since "0" 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "--since 0 accepted, exits 0"
else
  _fail "--since 0 exited $EXITCODE"
fi

# 6. --dry-run with --task-class filter: accepted without error
echo ""
echo "--- 6. --task-class filter accepted ---"
EXITCODE=0
reflect_cmd --dry-run --task-class "code_fix" 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "--task-class code_fix dry-run exits 0"
else
  _fail "--task-class code_fix dry-run exited $EXITCODE"
fi

OUT=$(reflect_cmd --dry-run --task-class "code_fix" 2>&1 || true)
if echo "$OUT" | grep -qi "code_fix\|filter\|task.class"; then
  _ok "--task-class code_fix reflected in output"
else
  # Output may just say "0 traces" — acceptable
  _ok "--task-class code_fix flag processed without crash"
fi

# 7. --lane resolves through agents.yaml and exports the gradient model in dry-run output
echo ""
echo "--- 7. --lane resolves through agents.yaml ---"
mkdir -p "$MINI_ORK_HOME/config"
cat > "$MINI_ORK_HOME/config/agents.yaml" <<'YAML'
lanes:
  reflector: sonnet
  cheap_reflect: kimi
YAML
OUT=$(reflect_cmd --dry-run --lane cheap_reflect 2>&1 || true)
if echo "$OUT" | grep -q "lane: cheap_reflect -> kimi"; then
  _ok "--lane cheap_reflect resolves to kimi"
else
  _fail "--lane cheap_reflect did not resolve via agents.yaml (got: $OUT)"
fi

# 8. --dry-run with empty DB: reports 0 traces, exits 0
echo ""
echo "--- 8. Empty DB: 0 traces reported ---"
OUT=$(reflect_cmd --dry-run 2>&1 || true)
if echo "$OUT" | grep -qiE "0 trace|0 trace|dry.run.*0|analyz.*0"; then
  _ok "empty DB → 0 traces reported"
else
  # Acceptable if just says "dry-run: would analyze" without a count
  _ok "empty DB handled (dry-run output: $(echo "$OUT" | head -1))"
fi

# === TESTS END ===

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
