#!/usr/bin/env bash
# tests/security/test_sec_recursive_spawn_limits.sh — recursion policy hardening
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=1

TMPROOT=$(mktemp -d /tmp/ork-spawn-sec-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT" || exit 1
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

mini-ork init >/dev/null 2>&1

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

sqlite3 "$MINI_ORK_DB" "
  INSERT INTO task_runs(id, task_class, recipe, kickoff_path, status, created_at, updated_at)
  VALUES ('parent-sec', 'code_fix', 'code-fix', '$TMPROOT/parent.md', 'classified', strftime('%s','now'), strftime('%s','now'));
"

cat > "$TMPROOT/child.md" <<'EOF'
# Security child
## Definition of Done
- dry run only.
## Scope
- temp files only.
EOF

echo "── security: recursive spawn limits ──"

echo ""
echo "--- 1. depth limit blocks over-deep child ---"
export MINI_ORK_RECURSIVE_MAX_DEPTH=1
EXITCODE=0
mini-ork-spawn --parent-run parent-sec --kickoff "$TMPROOT/child.md" --recipe code-fix --child-run child-too-deep --depth 2 --no-execute >/tmp/spawn-depth.err 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -ne 0 ] && grep -q "max_depth" /tmp/spawn-depth.err; then
  _ok "over-depth spawn blocked"
else
  _fail "over-depth spawn should block, exit=$EXITCODE, output=$(cat /tmp/spawn-depth.err)"
fi

echo ""
echo "--- 2. full authority is not allowed by default ---"
unset MINI_ORK_RECURSIVE_MAX_DEPTH
EXITCODE=0
mini-ork-spawn --parent-run parent-sec --kickoff "$TMPROOT/child.md" --recipe code-fix --child-run child-root-authority --authority 1.0 --no-execute >/tmp/spawn-authority.err 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -ne 0 ] && grep -q "authority_level 1.0" /tmp/spawn-authority.err; then
  _ok "authority 1.0 blocked"
else
  _fail "authority 1.0 should block, exit=$EXITCODE, output=$(cat /tmp/spawn-authority.err)"
fi

echo ""
echo "--- 3. missing parent cannot create orphan lineage ---"
EXITCODE=0
mini-ork-spawn --parent-run missing-parent --kickoff "$TMPROOT/child.md" --recipe code-fix --child-run child-orphan --no-execute >/tmp/spawn-orphan.err 2>&1 || EXITCODE=$?
if [ "$EXITCODE" -ne 0 ] && grep -q "parent task_run not found" /tmp/spawn-orphan.err; then
  _ok "orphan spawn blocked"
else
  _fail "orphan spawn should block, exit=$EXITCODE, output=$(cat /tmp/spawn-orphan.err)"
fi

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
