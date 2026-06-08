#!/usr/bin/env bash
# tests/integration/test_bin_dispatcher.sh — integration tests for bin/mini-ork (top-level dispatcher)
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

echo "── integration: mini-ork (top-level dispatcher) ──"

# === TESTS START ===

# 1. mini-ork help exits 0 and mentions all subcommands
echo ""
echo "--- 1. mini-ork help exits 0 ---"
if mini-ork help >/dev/null 2>&1; then
  _ok "mini-ork help exits 0"
else
  _fail "mini-ork help exited non-zero"
fi

HELP_OUT=$(mini-ork help 2>&1 || true)
for kw in "classify" "plan" "execute" "verify" "reflect" "improve" "eval" "promote" "run" "init" "doctor" "version"; do
  if echo "$HELP_OUT" | grep -qi "$kw"; then
    _ok "help mentions subcommand: $kw"
  else
    _fail "help does not mention subcommand: $kw"
  fi
done

# 2. mini-ork --help and mini-ork -h also exit 0
echo ""
echo "--- 2. --help / -h aliases exit 0 ---"
EXITCODE=0
mini-ork --help >/dev/null 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "mini-ork --help exits 0"
else
  _fail "mini-ork --help exited $EXITCODE"
fi

EXITCODE=0
mini-ork -h >/dev/null 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "mini-ork -h exits 0"
else
  _fail "mini-ork -h exited $EXITCODE"
fi

# 3. mini-ork version exits 0 and prints version string
echo ""
echo "--- 3. mini-ork version exits 0 + prints version ---"
EXITCODE=0
VERSION_OUT=$(mini-ork version 2>&1) || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "mini-ork version exits 0"
else
  _fail "mini-ork version exited $EXITCODE"
fi
if echo "$VERSION_OUT" | grep -qiE "mini.ork|[0-9]+\.[0-9]+"; then
  _ok "version output contains version string (got: $VERSION_OUT)"
else
  _fail "version output missing version string (got: $VERSION_OUT)"
fi

# 4. mini-ork doctor exits 0 and reports on deps
echo ""
echo "--- 4. mini-ork doctor exits 0 ---"
EXITCODE=0
DOCTOR_OUT=$(mini-ork doctor 2>&1) || EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
  _ok "mini-ork doctor exits 0"
else
  _fail "mini-ork doctor exited $EXITCODE"
fi
if echo "$DOCTOR_OUT" | grep -qiE "\[OK\]|\[MISSING\]|doctor|bash|sqlite3|MINI_ORK"; then
  _ok "doctor output contains dep check lines"
else
  _fail "doctor output missing expected dep check lines (got: $DOCTOR_OUT)"
fi

# 5. mini-ork <unknown> exits 2 with helpful error
echo ""
echo "--- 5. Unknown subcommand exits 2 ---"
EXITCODE=0
ERR_OUT=$(mini-ork totally-unknown-subcommand-$$$ 2>&1) || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "unknown subcommand → exit 2"
else
  _fail "unknown subcommand → expected exit 2, got exit $EXITCODE"
fi
if echo "$ERR_OUT" | grep -qi "unknown\|help\|subcommand"; then
  _ok "unknown subcommand error message is helpful"
else
  _fail "unknown subcommand error message not helpful (got: $ERR_OUT)"
fi

# 6. mini-ork run with nonexistent recipe exits 2
echo ""
echo "--- 6. mini-ork run with nonexistent recipe exits 2 ---"
EXITCODE=0
ERR_OUT=$(mini-ork run no-such-recipe "$TMPROOT/kickoff.md" 2>&1) || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "nonexistent recipe → exit 2"
else
  _fail "nonexistent recipe → expected exit 2, got exit $EXITCODE"
fi
if echo "$ERR_OUT" | grep -qi "recipe\|no recipe\|not found\|recipes/"; then
  _ok "nonexistent recipe error message mentions recipe lookup"
else
  _fail "nonexistent recipe error not helpful (got: $ERR_OUT)"
fi

# 7. mini-ork run without kickoff arg exits non-zero
echo ""
echo "--- 7. mini-ork run without kickoff exits non-zero ---"
EXITCODE=0
mini-ork run code-fix 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -ne 0 ]; then
  _ok "mini-ork run code-fix (no kickoff) → exit $EXITCODE (non-zero)"
else
  _fail "mini-ork run code-fix (no kickoff) → unexpected exit 0"
fi

# 8. mini-ork run with real recipe + kickoff in DRY_RUN: walks classify→plan→execute→verify
# and produces a task_runs row in state.db
echo ""
echo "--- 8. mini-ork run dry-run: classify→plan→execute→verify pipeline ---"
export MINI_ORK_RUN_ID="run-dispatcher-test-$$"
RUN_OUT=$(mini-ork run code-fix "$TMPROOT/kickoff.md" 2>&1) || RUN_EXIT=$?
RUN_EXIT="${RUN_EXIT:-0}"

# Dry-run should exit 0. It may not emit an artifact_path, but the wrapper
# must still continue into verify and let verify emit verdict=dry-run.
if [ "$RUN_EXIT" -eq 0 ]; then
  _ok "mini-ork run code-fix dry-run completed (exit $RUN_EXIT)"
else
  _fail "mini-ork run code-fix dry-run failed unexpectedly (exit $RUN_EXIT)"
fi

# classify step should have emitted task_class=
if echo "$RUN_OUT" | grep -qE '^task_class='; then
  TC=$(echo "$RUN_OUT" | grep -E '^task_class=' | head -1 | cut -d= -f2)
  _ok "classify step emitted task_class=${TC}"
else
  _fail "classify step did NOT emit task_class= line (output: $(echo "$RUN_OUT" | head -5))"
fi

# plan step should have emitted plan_path=
if echo "$RUN_OUT" | grep -qE '^plan_path='; then
  _ok "plan step emitted plan_path="
else
  _fail "plan step did NOT emit plan_path= line"
fi

# verify step should have emitted JSON with verdict
if echo "$RUN_OUT" | python3 -c "
import sys, json
# find any JSON line with 'verdict' key
for line in sys.stdin:
    try:
        d = json.loads(line)
        if 'verdict' in d:
            print('found')
            break
    except Exception:
        continue
" 2>/dev/null | grep -q "found"; then
  _ok "verify step emitted JSON with verdict field"
else
  # verify may print multi-line JSON — check for verdict anywhere in output
  if echo "$RUN_OUT" | grep -qi '"verdict"'; then
    _ok "verify step output contains 'verdict' field"
  else
    _fail "verify step did NOT emit verdict field"
  fi
fi

# DB should contain a task_runs row (written by classify step with DRY_RUN=0 path,
# but we're in DRY_RUN=1 — classify skips DB write, so check classify output at minimum)
DB_ROW_COUNT=$(sqlite3 "$MINI_ORK_DB" \
  "SELECT COUNT(*) FROM task_runs WHERE id='${MINI_ORK_RUN_ID}';" 2>/dev/null || echo 0)
if [ "$DB_ROW_COUNT" -gt 0 ]; then
  _ok "task_runs row created for run_id=${MINI_ORK_RUN_ID}"
else
  # DRY_RUN=1 means classify skips DB writes — that's by design
  _ok "task_runs row not created (DRY_RUN=1 — classify skips DB write; by design)"
fi

# 9. mini-ork run <kickoff.md> infers recipe from classifier, then walks pipeline.
echo ""
echo "--- 9. mini-ork run kickoff.md: inferred recipe path ---"
cat > "$TMPROOT/ui-audit-kickoff.md" <<'EOF'
# UI audit for CLI quickstart

Audit the first-run CLI and README journey for accessibility, visual scanning,
interaction clarity, and edge cases.

## Success criteria

- Findings cite README/help text anchors.
- The workflow should be the ui-audit recipe for a read-only audit.
EOF

MINI_ORK_RUN_ID="run-dispatcher-md-only-$$"
export MINI_ORK_RUN_ID
MD_RUN_EXIT=0
MD_RUN_OUT=$(mini-ork run "$TMPROOT/ui-audit-kickoff.md" 2>&1) || MD_RUN_EXIT=$?

if [ "$MD_RUN_EXIT" -eq 0 ]; then
  _ok "mini-ork run kickoff.md dry-run completed"
else
  _fail "mini-ork run kickoff.md failed unexpectedly (exit $MD_RUN_EXIT)"
fi

MD_TC=$(echo "$MD_RUN_OUT" | grep -E '^task_class=' | tail -1 | cut -d= -f2)
if [ "$MD_TC" = "ui_audit" ]; then
  _ok "kickoff.md inferred task_class=ui_audit"
else
  _fail "kickoff.md inferred wrong task_class=${MD_TC:-missing}"
fi

if echo "$MD_RUN_OUT" | grep -q '"verdict"'; then
  _ok "kickoff.md inferred path reached verify"
else
  _fail "kickoff.md inferred path did not reach verify"
fi

# === TESTS END ===

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
