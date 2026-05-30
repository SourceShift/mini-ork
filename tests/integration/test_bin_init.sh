#!/usr/bin/env bash
# tests/integration/test_bin_init.sh — integration tests for bin/mini-ork-init
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export PATH="$MINI_ORK_ROOT/bin:$PATH"
export MINI_ORK_DRY_RUN=0   # init is always run live; dry-run irrelevant here

# Isolated tmp project — DO NOT pre-init (this file tests init itself)
TMPROOT=$(mktemp -d /tmp/ork-int-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT"
git init -q
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

echo "── integration: mini-ork-init ──"

# === TESTS START ===

# 1. --help exits 0 and prints usage
echo ""
echo "--- 1. --help exits 0 ---"
if mini-ork-init --help >/dev/null 2>&1; then
  _ok "--help exits 0"
else
  _fail "--help exited non-zero"
fi

HELP_OUT=$(mini-ork-init --help 2>&1 || true)
if echo "$HELP_OUT" | grep -qi "init\|usage\|mini-ork\|bootstrap\|scaffold"; then
  _ok "--help output mentions init/usage keywords"
else
  _fail "--help output does not mention expected keywords (got: $HELP_OUT)"
fi

# 2. Happy path: init creates .mini-ork/ directory structure
echo ""
echo "--- 2. Happy path: directory structure ---"
mini-ork-init >/dev/null 2>&1
if [ -d "$MINI_ORK_HOME" ]; then
  _ok ".mini-ork/ directory created"
else
  _fail ".mini-ork/ directory NOT created"
fi

for subdir in kickoffs INBOX runs locks secrets config; do
  if [ -d "$MINI_ORK_HOME/$subdir" ]; then
    _ok ".mini-ork/$subdir/ exists"
  else
    _fail ".mini-ork/$subdir/ missing"
  fi
done

# 3. state.db is created and has migrations applied
echo ""
echo "--- 3. state.db created + migrations applied ---"
if [ -f "$MINI_ORK_DB" ]; then
  _ok "state.db file exists at $MINI_ORK_DB"
else
  _fail "state.db NOT found at $MINI_ORK_DB"
fi

MIGRATION_COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM schema_migrations;" 2>/dev/null || echo 0)
if [ "$MIGRATION_COUNT" -gt 0 ]; then
  _ok "schema_migrations has $MIGRATION_COUNT rows (migrations applied)"
else
  _fail "schema_migrations is empty (0 rows) — migrations may have failed"
fi

# 4. agents.yaml and task_classes/ exist in config
echo ""
echo "--- 4. config files seeded ---"
if [ -f "$MINI_ORK_HOME/config/agents.yaml" ]; then
  _ok "config/agents.yaml exists"
else
  _fail "config/agents.yaml NOT found"
fi

if [ -d "$MINI_ORK_HOME/config/task_classes" ]; then
  _ok "config/task_classes/ directory exists"
else
  _fail "config/task_classes/ NOT found"
fi

# 5. .gitignore updated with .mini-ork entries
echo ""
echo "--- 5. .gitignore updated ---"
GITIGNORE="$TMPROOT/.gitignore"
if [ -f "$GITIGNORE" ]; then
  _ok ".gitignore file exists"
else
  _fail ".gitignore was NOT created"
fi

for entry in ".mini-ork/state.db" ".mini-ork/runs/" ".mini-ork/secrets/"; do
  if grep -qxF "$entry" "$GITIGNORE" 2>/dev/null; then
    _ok ".gitignore contains: $entry"
  else
    _fail ".gitignore missing: $entry"
  fi
done

# 6. Idempotency: re-running init must not fail
echo ""
echo "--- 6. Idempotency: re-run exits 0 ---"
if mini-ork-init >/dev/null 2>&1; then
  _ok "second mini-ork-init run exits 0 (idempotent)"
else
  _fail "second mini-ork-init run exited non-zero"
fi

# 7. task_class yaml(s) seeded from recipes/
echo ""
echo "--- 7. task_class yamls seeded from recipes ---"
TASK_CLASS_COUNT=$(ls "$MINI_ORK_HOME/config/task_classes/"*.yaml 2>/dev/null | wc -l | tr -d ' ')
if [ "$TASK_CLASS_COUNT" -gt 0 ]; then
  _ok "task_classes/ has $TASK_CLASS_COUNT yaml(s) seeded from recipes/"
else
  _fail "task_classes/ has no yaml files seeded"
fi

# === TESTS END ===

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
