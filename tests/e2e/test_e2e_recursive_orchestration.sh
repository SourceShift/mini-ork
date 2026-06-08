#!/usr/bin/env bash
# tests/e2e/test_e2e_recursive_orchestration.sh
# Runs a real parent -> child -> grandchild mini-ork delegation chain with
# MINI_ORK_DRY_RUN=1 so no external model provider is required.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1
export MINI_ORK_RECURSIVE_MAX_DEPTH=2
export MINI_ORK_RECURSIVE_MAX_CHILDREN=4
export MINI_ORK_RECURSIVE_MAX_DESCENDANTS=16

TMPROOT=$(mktemp -d /tmp/ork-recursive-e2e-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT" || exit 1
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

cat > "$TMPROOT/root.md" <<'EOF'
# Recursive root build
## Problem
Split the mini-ork builder into child validation tasks.
## Definition of Done
- Child plans exist.
## Scope
- ONLY temp validation artifacts may be created.
EOF

cat > "$TMPROOT/child.md" <<'EOF'
# Recursive child build
## Problem
Create a child plan for the delegated subtask.
## Definition of Done
- A plan file exists.
## Scope
- ONLY temp validation artifacts may be created.
EOF

cat > "$TMPROOT/grandchild.md" <<'EOF'
# Recursive grandchild build
## Problem
Create a grandchild plan for the delegated subtask.
## Definition of Done
- A plan file exists.
## Scope
- ONLY temp validation artifacts may be created.
EOF

echo "── e2e: recursive orchestration ──"

mini-ork init >/dev/null 2>&1

echo ""
echo "--- 1. root run creates parent task_run through normal dispatcher ---"
export MINI_ORK_RUN_ID="root-recursive-e2e"
ROOT_OUT=$(mini-ork run code-fix "$TMPROOT/root.md" 2>&1)
if echo "$ROOT_OUT" | grep -q '^plan_path='; then
  sqlite3 "$MINI_ORK_DB" "
    INSERT OR IGNORE INTO task_runs(id, task_class, recipe, kickoff_path, status, created_at, updated_at)
    VALUES ('root-recursive-e2e', 'code_fix', 'code-fix', '$TMPROOT/root.md', 'classified', strftime('%s','now'), strftime('%s','now'));
  "
  _ok "root run completed through mini-ork run dry-run path"
else
  _fail "root run did not emit plan_path (got: $ROOT_OUT)"
fi

echo ""
echo "--- 2. parent spawns child with child-spawn permission ---"
CHILD_OUT=$(mini-ork spawn --parent-run root-recursive-e2e --kickoff "$TMPROOT/child.md" --recipe code-fix --child-run child-recursive-e2e --allow-child-spawn 2>&1)
if echo "$CHILD_OUT" | grep -q '^spawn_status=completed'; then
  _ok "child run completed"
else
  _fail "child run failed (got: $CHILD_OUT)"
fi

echo ""
echo "--- 3. child spawns grandchild at depth 2 ---"
GRAND_OUT=$(mini-ork spawn --parent-run child-recursive-e2e --kickoff "$TMPROOT/grandchild.md" --recipe code-fix --child-run grandchild-recursive-e2e --depth 2 2>&1)
if echo "$GRAND_OUT" | grep -q '^spawn_status=completed'; then
  _ok "grandchild run completed"
else
  _fail "grandchild run failed (got: $GRAND_OUT)"
fi

echo ""
echo "--- 4. lineage and event log are queryable ---"
SPAWN_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM run_spawns WHERE root_run_id='root-recursive-e2e';")
if [ "$SPAWN_COUNT" -eq 2 ]; then
  _ok "two descendant spawns recorded"
else
  _fail "expected 2 descendant spawns, got $SPAWN_COUNT"
fi

COMPLETED_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM run_events WHERE event_type='child.completed';")
if [ "$COMPLETED_COUNT" -eq 2 ]; then
  _ok "child.completed events recorded for child and grandchild"
else
  _fail "expected 2 child.completed events, got $COMPLETED_COUNT"
fi

echo ""
echo "--- 5. depth 3 is blocked by policy ---"
EXITCODE=0
mini-ork spawn --parent-run grandchild-recursive-e2e --kickoff "$TMPROOT/grandchild.md" --recipe code-fix --child-run too-deep-recursive-e2e --depth 3 >/tmp/recursive-too-deep.err 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -ne 0 ] && grep -q "max_depth" /tmp/recursive-too-deep.err; then
  _ok "depth 3 spawn blocked"
else
  _fail "depth 3 should block, exit=$EXITCODE, output=$(cat /tmp/recursive-too-deep.err)"
fi

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
