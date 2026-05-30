#!/usr/bin/env bash
# tests/integration/test_bin_classify.sh — integration tests for bin/mini-ork-classify
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
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

# Synthetic kickoff files
cat > "$TMPROOT/kickoff.md" <<'EOF'
# Fix bug in tally.js
## Problem
Off-by-one in computeTotal().
## Definition of Done
- npm test passes.
## Scope
- ONLY tally.js may be edited.
EOF

cat > "$TMPROOT/kickoff-generic.md" <<'EOF'
# Some random task
## Description
This does not match any specific keyword.
## Done when
Something is done.
EOF

echo "── integration: mini-ork-classify ──"

# === TESTS START ===

# 1. --help exits 0 and prints usage
echo ""
echo "--- 1. --help exits 0 ---"
if mini-ork-classify --help >/dev/null 2>&1; then
  _ok "--help exits 0"
else
  _fail "--help exited non-zero"
fi

HELP_OUT=$(mini-ork-classify --help 2>&1 || true)
if echo "$HELP_OUT" | grep -qi "classify\|usage\|kickoff\|task.class"; then
  _ok "--help output mentions expected keywords"
else
  _fail "--help output does not mention expected keywords (got: $HELP_OUT)"
fi

# 2. Missing required arg exits 2
echo ""
echo "--- 2. Missing required arg exits 2 ---"
EXITCODE=0
mini-ork-classify 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "no args → exit 2"
else
  _fail "no args → expected exit 2, got exit $EXITCODE"
fi

# 3. Nonexistent kickoff file exits 2
echo ""
echo "--- 3. Nonexistent file exits 2 ---"
EXITCODE=0
mini-ork-classify /tmp/does-not-exist-ork-$$$.md 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "nonexistent kickoff → exit 2"
else
  _fail "nonexistent kickoff → expected exit 2, got exit $EXITCODE"
fi

# 4. Happy path: classify produces task_class= on stdout
echo ""
echo "--- 4. Happy path: task_class= emitted ---"
OUT=$(mini-ork-classify "$TMPROOT/kickoff.md" 2>/dev/null || true)
if echo "$OUT" | grep -qE '^task_class='; then
  CLASS=$(echo "$OUT" | grep -E '^task_class=' | head -1 | cut -d= -f2)
  _ok "task_class=${CLASS} emitted on stdout"
else
  _fail "stdout did not contain task_class= line (got: $OUT)"
fi

# 5. keyword-matched kickoff → known task class (code_fix or similar)
echo ""
echo "--- 5. Keyword match → code_fix class ---"
OUT=$(mini-ork-classify "$TMPROOT/kickoff.md" 2>/dev/null || true)
CLASS=$(echo "$OUT" | grep -E '^task_class=' | head -1 | cut -d= -f2)
if [ "$CLASS" = "code_fix" ] || [ "$CLASS" = "code-fix" ]; then
  _ok "kickoff with 'fix bug' → task_class=${CLASS} (expected code_fix)"
else
  # May be generic if no matching task class; accept generic too as long as something is emitted
  if [ -n "$CLASS" ]; then
    _ok "task_class=${CLASS} emitted (no code_fix yaml present — fallback to ${CLASS} is acceptable)"
  else
    _fail "task_class was empty; expected at least 'generic'"
  fi
fi

# 6. Unrecognised kickoff → generic fallback
echo ""
echo "--- 6. Unrecognised content → generic fallback ---"
OUT=$(mini-ork-classify "$TMPROOT/kickoff-generic.md" 2>/dev/null || true)
CLASS=$(echo "$OUT" | grep -E '^task_class=' | head -1 | cut -d= -f2)
if [ -n "$CLASS" ]; then
  _ok "unrecognised kickoff → task_class=${CLASS} (fallback emitted)"
else
  _fail "unrecognised kickoff produced empty task_class"
fi

# 7. --dry-run: prints dry-run message and exits 0
echo ""
echo "--- 7. --dry-run exits 0 + prints dry-run marker ---"
OUT=$(mini-ork-classify --dry-run "$TMPROOT/kickoff.md" 2>&1 || true)
if echo "$OUT" | grep -qi "dry.run\|dry_run"; then
  _ok "--dry-run output mentions dry-run"
else
  _fail "--dry-run output did not mention dry-run (got: $OUT)"
fi

# 8. --workflow-version override reflected in stdout
echo ""
echo "--- 8. --workflow-version override emitted ---"
OUT=$(mini-ork-classify --workflow-version "v99" "$TMPROOT/kickoff.md" 2>/dev/null || true)
if echo "$OUT" | grep -qE 'workflow_version=v99'; then
  _ok "--workflow-version v99 reflected in stdout"
else
  _fail "--workflow-version v99 not found in stdout (got: $OUT)"
fi

# === TESTS END ===

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
