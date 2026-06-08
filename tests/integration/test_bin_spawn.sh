#!/usr/bin/env bash
# tests/integration/test_bin_spawn.sh — integration tests for bin/mini-ork-spawn
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1

TMPROOT=$(mktemp -d /tmp/ork-spawn-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT" || exit 1
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

mini-ork init >/dev/null 2>&1

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

seed_parent() {
  local run_id="$1"
  sqlite3 "$MINI_ORK_DB" "
    INSERT OR REPLACE INTO task_runs(id, task_class, recipe, kickoff_path, status, created_at, updated_at)
    VALUES ('$run_id', 'code_fix', 'code-fix', '$TMPROOT/parent.md', 'classified', strftime('%s','now'), strftime('%s','now'));
  "
}

cat > "$TMPROOT/parent.md" <<'EOF'
# Parent recursive validation run
## Definition of Done
- Child runs produce plans.
## Scope
- Only temp files may be touched.
EOF

cat > "$TMPROOT/child.md" <<'EOF'
# Child code fix
## Problem
Fix a tiny deterministic issue in demo.py.
## Definition of Done
- pytest passes.
## Scope
- ONLY demo.py may be edited.
EOF

echo "── integration: mini-ork-spawn ──"

echo ""
echo "--- 1. --help exits 0 ---"
if mini-ork-spawn --help >/dev/null 2>&1; then
  _ok "--help exits 0"
else
  _fail "--help exited non-zero"
fi

echo ""
echo "--- 2. missing args exits 2 ---"
EXITCODE=0
mini-ork-spawn 2>/dev/null || EXITCODE=$?
if [ "$EXITCODE" -eq 2 ]; then
  _ok "missing args -> exit 2"
else
  _fail "missing args expected exit 2, got $EXITCODE"
fi

echo ""
echo "--- 3. --no-execute records approved spawn ---"
seed_parent "parent-int-1"
OUT=$(mini-ork-spawn --parent-run parent-int-1 --kickoff "$TMPROOT/child.md" --recipe code-fix --child-run child-int-1 --no-execute 2>&1)
if echo "$OUT" | grep -q '^spawn_status=approved'; then
  _ok "approved spawn status emitted"
else
  _fail "approved spawn status missing (got: $OUT)"
fi

ROW=$(sqlite3 "$MINI_ORK_DB" "SELECT parent_run_id || '|' || child_run_id || '|' || status FROM run_spawns WHERE child_run_id='child-int-1';")
if [ "$ROW" = "parent-int-1|child-int-1|approved" ]; then
  _ok "run_spawns row written"
else
  _fail "unexpected run_spawns row: $ROW"
fi

EVENT_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM run_events WHERE run_id='child-int-1' AND event_type='spawn.approved';")
if [ "$EVENT_COUNT" -eq 1 ]; then
  _ok "spawn.approved event written"
else
  _fail "spawn.approved event count=$EVENT_COUNT"
fi

echo ""
echo "--- 4. execute path runs child mini-ork in isolated workspace ---"
seed_parent "parent-int-2"
OUT=$(mini-ork-spawn --parent-run parent-int-2 --kickoff "$TMPROOT/child.md" --recipe code-fix --child-run child-int-2 2>&1)
if echo "$OUT" | grep -q '^spawn_status=completed'; then
  _ok "child run completed"
else
  _fail "child run did not complete (got: $OUT)"
fi

WORKSPACE=$(echo "$OUT" | grep -E '^child_workspace=' | cut -d= -f2- | head -1)
if [ -n "$WORKSPACE" ] && [ -d "$WORKSPACE" ]; then
  _ok "child workspace exists"
else
  _fail "child workspace missing: $WORKSPACE"
fi

PLAN_PATH="$MINI_ORK_HOME/runs/child-int-2/plan.json"
if [ -f "$PLAN_PATH" ]; then
  _ok "child plan created in shared run home"
else
  _fail "child plan missing: $PLAN_PATH"
fi

echo ""
echo "--- 5. child cap blocks fifth spawn ---"
seed_parent "parent-int-cap"
export MINI_ORK_RECURSIVE_MAX_CHILDREN=4
for n in 1 2 3 4; do
  mini-ork-spawn --parent-run parent-int-cap --kickoff "$TMPROOT/child.md" --recipe code-fix --child-run "child-cap-$n" --no-execute >/dev/null 2>&1 || true
done
EXITCODE=0
mini-ork-spawn --parent-run parent-int-cap --kickoff "$TMPROOT/child.md" --recipe code-fix --child-run child-cap-5 --no-execute >/tmp/spawn-cap.err 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -ne 0 ] && grep -q "max_children_per_run" /tmp/spawn-cap.err; then
  _ok "fifth child blocked by max_children_per_run"
else
  _fail "fifth child should block, exit=$EXITCODE, output=$(cat /tmp/spawn-cap.err)"
fi

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
