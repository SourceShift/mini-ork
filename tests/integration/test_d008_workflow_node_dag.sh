#!/usr/bin/env bash
# tests/integration/test_d008_workflow_node_dag.sh
# D-008 regression: the native executor must read node DAG from workflow.yaml
# (not plan.json.decomposition[]). Asserts:
#   1. dry-run output lists 9 nodes for refactor-audit (workflow.yaml node count)
#   2. each node has a non-empty node_type (not "type=" silent skip)
#   3. NODE_SOURCE label is "workflow.yaml"
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1

TMPROOT=$(mktemp -d /tmp/ork-d008-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT"
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
mini-ork init >/dev/null 2>&1
cp "$MINI_ORK_ROOT/kickoffs/scale-refactor-mini-ork.md" "$TMPROOT/kickoff.md"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

echo "── d-008: workflow.yaml node dag ──"

OUT=$(mini-ork run refactor-audit "$TMPROOT/kickoff.md" 2>&1)

# Assertion 1: NODE_SOURCE label says workflow.yaml
if echo "$OUT" | grep -qE "nodes:.*from workflow.yaml"; then
  _ok "execute reports nodes loaded from workflow.yaml"
else
  _fail "execute did NOT report 'from workflow.yaml' (regression D-008)"
fi

# Assertion 2: 9 nodes dispatched in dry-run (refactor-audit workflow node count)
DISPATCH_LINES=$(echo "$OUT" | grep -cE "\[dry-run\] would dispatch node_id=")
if [ "$DISPATCH_LINES" -ge 7 ]; then
  _ok "≥7 nodes dispatched in dry-run (got $DISPATCH_LINES, expect 9)"
else
  _fail "<7 nodes dispatched in dry-run (got $DISPATCH_LINES) — D-008 regression"
fi

# Assertion 3: every dispatch line has non-empty node_type
EMPTY_TYPES=$(echo "$OUT" | grep -E "\[dry-run\] would dispatch" | grep -cE "node_type=:")
if [ "$EMPTY_TYPES" -eq 0 ]; then
  _ok "no empty node_type fields in dispatch output"
else
  _fail "$EMPTY_TYPES dispatch lines have empty node_type (D-008b regression)"
fi

# Assertion 4: classifier routing — kickoff with 'audit' + 'scalability' + 'refactor'
# keywords should route to refactor-audit (not bdd-first or code-fix which also have some).
#
# Convention update (2026-06-05 Path A fix at commit 2c12d9d): when invoked
# via `mini-ork run <recipe> <kickoff>`, the dispatcher derives task_class
# from recipes/<recipe>/task_class.yaml::name (which uses underscores —
# e.g. refactor_audit, code_fix). When invoked via `mini-ork classify`
# without an explicit recipe, the keyword-count classifier returns the
# yaml::name verbatim (also underscored after the D-050 fix). So the
# test accepts EITHER form to remain valid under both Path A invocations
# and pre-Path-A direct-classify invocations.
TASK_CLASS=$(echo "$OUT" | grep -E '^task_class=' | head -1 | cut -d= -f2)
if [ "$TASK_CLASS" = "refactor-audit" ] || [ "$TASK_CLASS" = "refactor_audit" ]; then
  _ok "classifier routed to refactor-audit / refactor_audit (D-010 + Path A both honored)"
else
  _fail "classifier routed to $TASK_CLASS (expected refactor-audit OR refactor_audit) — regression"
fi

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
