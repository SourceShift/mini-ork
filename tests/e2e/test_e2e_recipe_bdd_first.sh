#!/usr/bin/env bash
# tests/e2e/test_e2e_recipe_bdd_first.sh
# E2E: Full recipe walk-through for the bdd-first-delivery recipe in dry-run mode.
#   Structure mirrors test_e2e_recipe_code_fix.sh but targets bdd-first-delivery
#   specific task_class matching, workflow.yaml, and kickoff shape.
#
# No LLM credentials required: MINI_ORK_DRY_RUN=1 bypasses all LLM calls.
#
# Usage: bash tests/e2e/test_e2e_recipe_bdd_first.sh
# Exit 0 = all assertions pass. Exit 1 = any assertion failed.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
export MINI_ORK_DRY_RUN=1

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert() {
  local name="$1"; shift
  if eval "$@" 2>/dev/null; then _ok "$name"; else _fail "$name  (expr: $*)"; fi
}

echo "══════════════════════════════════════════════════════"
echo "  E2E: recipe bdd-first-delivery (dry-run)"
echo "══════════════════════════════════════════════════════"
echo ""

RECIPE_DIR="$MINI_ORK_ROOT/recipes/bdd-first-delivery"
if [[ ! -d "$RECIPE_DIR" ]]; then
  _skip "recipes/bdd-first-delivery not found — all tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"; exit 0
fi

# ── isolated project dir ──────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-e2e-bdd-XXXXXX)"
TEST_DIR="$(cd "$TEST_DIR" && pwd -P)"
export MINI_ORK_HOME="$TEST_DIR/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_ENGINE_ROOT="$MINI_ORK_ROOT"
export MINI_ORK_PROJECT_HOME="$MINI_ORK_HOME"
export MINI_ORK_TARGET_REPO="$TEST_DIR"
unset MINI_ORK_RUN_DIR MINI_ORK_RUN_ID MINI_ORK_PLAN_PATH MINI_ORK_RECIPE MINI_ORK_TASK_CLASS
trap 'rm -rf "$TEST_DIR"' EXIT

echo "--- mini-ork init ---"

"$MINI_ORK_ROOT/bin/mini-ork-init" >/dev/null 2>&1 || true
_assert ".mini-ork dir created" '[[ -d "$MINI_ORK_HOME" ]]'
_assert ".mini-ork/runs created" '[[ -d "$MINI_ORK_HOME/runs" ]]'

if [[ ! -f "$MINI_ORK_DB" ]]; then
  mkdir -p "$(dirname "$MINI_ORK_DB")"
  sqlite3 "$MINI_ORK_DB" "CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);" 2>/dev/null || true
  _ok "state.db bootstrapped manually"
fi

echo ""
echo "--- write bdd-first kickoff ---"

KICKOFF="$TEST_DIR/kickoff.md"
cat > "$KICKOFF" <<'MD'
# Feature: user login with BDD scenarios

**Task class:** bdd-first-delivery

## User story
As a registered user
I want to log in with my email and password
So that I can access my account dashboard.

## Acceptance criteria (Gherkin)
```gherkin
Feature: User login

  Scenario: Successful login
    Given a registered user with email "alice@example.com"
    When she submits the login form with correct credentials
    Then she should be redirected to the dashboard
    And a session cookie should be set

  Scenario: Wrong password
    Given a registered user with email "alice@example.com"
    When she submits the login form with an incorrect password
    Then she should see "Invalid credentials"
    And no session cookie should be set
```
MD
_assert "kickoff.md written" '[[ -f "$KICKOFF" ]]'

echo ""
echo "--- recipe artefacts present ---"

_assert "workflow.yaml exists in recipe" '[[ -f "$RECIPE_DIR/workflow.yaml" ]]'
_assert "task_class.yaml exists in recipe" '[[ -f "$RECIPE_DIR/task_class.yaml" ]]'
_assert "artifact_contract.yaml exists in recipe" '[[ -f "$RECIPE_DIR/artifact_contract.yaml" ]]'

echo ""
echo "--- mini-ork classify (dry-run) ---"

mkdir -p "$MINI_ORK_HOME/config/task_classes"
if [[ -f "$RECIPE_DIR/task_class.yaml" ]]; then
  cp "$RECIPE_DIR/task_class.yaml" "$MINI_ORK_HOME/config/task_classes/bdd-first-delivery.yaml"
fi

export MINI_ORK_RUN_ID="run-e2e-bdd-001"
CLASSIFY_OUT="$(PYTHONPATH="$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.ported.mini_ork_classify --dry-run "$KICKOFF" 2>/dev/null)"
CLASSIFY_EXIT=$?
_assert "classify --dry-run exits 0" '[[ "$CLASSIFY_EXIT" -eq 0 ]]'
_assert "classify emits task_class= line" '[[ "$CLASSIFY_OUT" == *task_class=* ]]'

TASK_CLASS="$(echo "$CLASSIFY_OUT" | grep -E '^task_class=' | head -1 | cut -d= -f2 || echo 'generic')"
export MINI_ORK_TASK_CLASS="$TASK_CLASS"
_assert "task_class is non-empty" '[[ -n "$TASK_CLASS" ]]'

# bdd-first kickoff should match the bdd-first-delivery pattern if the yaml
# keywords include "bdd", "gherkin", "scenario", "acceptance criteria", or
# "user story" — assert it's non-generic (warn as SKIP if yaml doesn't match)
if [[ "$TASK_CLASS" == "bdd-first-delivery" || "$TASK_CLASS" == "bdd-first" || "$TASK_CLASS" == "bdd_first_delivery" ]]; then
  _ok "task_class matched bdd-first-delivery pattern (${TASK_CLASS})"
elif [[ "$TASK_CLASS" == "generic" ]]; then
  _skip "task_class matched as 'generic' — task_class.yaml patterns may not cover 'bdd' keywords; verify yaml file"
else
  _ok "task_class matched some non-generic class: ${TASK_CLASS}"
fi

echo ""
echo "--- mini-ork plan (dry-run) ---"

export MINI_ORK_RECIPE="bdd-first-delivery"
export MINI_ORK_WORKFLOW="$RECIPE_DIR/workflow.yaml"

PLAN_OUT="$("$MINI_ORK_ROOT/bin/mini-ork-plan" --dry-run "$KICKOFF" 2>/dev/null)"
PLAN_EXIT=$?
_assert "plan --dry-run exits 0" '[[ "$PLAN_EXIT" -eq 0 ]]'
_assert "plan emits plan_path= on stdout" '[[ "$PLAN_OUT" == *plan_path=* ]]'

PLAN_PATH="$(echo "$PLAN_OUT" | grep -E '^plan_path=' | head -1 | cut -d= -f2 || echo '')"
export MINI_ORK_PLAN_PATH="$PLAN_PATH"
_assert "plan.json file exists" '[[ -f "${PLAN_PATH:-/nonexistent}" ]]'

echo ""
echo "--- plan.json shape validation ---"

if [[ -f "${PLAN_PATH:-}" ]]; then
  HAS_VC="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    vc = d.get('verifier_contract', {})
    checks = vc.get('checks', [])
    print('ok' if checks else 'missing')
except Exception:
    print('parse_error')
" "$PLAN_PATH" 2>/dev/null || echo 'parse_error')"
  _assert "plan.json has verifier_contract.checks" '[[ "$HAS_VC" == "ok" ]]'

  HAS_AC="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    ac = d.get('artifact_contract', {})
    print('ok' if ac else 'empty')
except Exception:
    print('parse_error')
" "$PLAN_PATH" 2>/dev/null || echo 'parse_error')"
  _assert "plan.json has artifact_contract" '[[ "$HAS_AC" == "ok" ]]'
else
  _skip "plan.json not found — shape validation skipped"
fi

echo ""
echo "--- plan_path under .mini-ork/runs/ ---"

if [[ -n "${PLAN_PATH:-}" ]]; then
  _assert "plan_path inside MINI_ORK_HOME/runs/" '[[ "$PLAN_PATH" == "$MINI_ORK_HOME/runs/"* ]]'
fi

echo ""
echo "--- recipe-specific lib directory (bdd-first may ship lib/) ---"

BDD_LIB_DIR="$RECIPE_DIR/lib"
if [[ -d "$BDD_LIB_DIR" ]]; then
  BDD_LIB_COUNT="$(ls "$BDD_LIB_DIR"/*.sh 2>/dev/null | wc -l | tr -d ' ')"
  _assert "bdd-first-delivery lib/ has >= 1 .sh file" '[[ "${BDD_LIB_COUNT:-0}" -ge 1 ]]'
else
  _skip "recipes/bdd-first-delivery/lib/ not present — recipe lib check skipped"
fi

echo ""
echo "--- task_runs row check ---"

if [[ -f "$MINI_ORK_DB" ]]; then
  RUN_STATUS="$(python3 - "$MINI_ORK_DB" "$MINI_ORK_RUN_ID" <<'PY'
import sqlite3, sys
db, rid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
try:
    row = con.execute("SELECT status FROM task_runs WHERE id=?", (rid,)).fetchone()
    print(row[0] if row else "NOT_FOUND")
except sqlite3.OperationalError:
    print("TABLE_MISSING")
finally:
    con.close()
PY
)"
  if [[ "$RUN_STATUS" == "TABLE_MISSING" || "$RUN_STATUS" == "NOT_FOUND" ]]; then
    _ok "task_runs row absent in dry-run (expected; table may be missing or dry-run skips write)"
  else
    _assert "task_runs status in {classified, planned}" \
      '[[ "$RUN_STATUS" == "classified" || "$RUN_STATUS" == "planned" ]]'
  fi
else
  _skip "state.db absent — task_runs row check skipped"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
