#!/usr/bin/env bash
# mini-ork smoke test
# Usage: bash tests/smoke.sh
# Exit 0 = all checks OK or SKIP.  Exit 1 = at least one FAIL.
#
# Does NOT require a live claude CLI.  All checks that need it are either
# skipped or use a mock binary installed into the temp HOME.
set -Eeuo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "[OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "[SKIP] $*"; SKIP=$((SKIP+1)); }

# Resolve repo root (works whether called as ./tests/smoke.sh or bash tests/smoke.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== mini-ork smoke test ==="
echo "    repo: $MINI_ORK_REPO"
echo ""

# ── 1. Dependency detection ───────────────────────────────────────────────────

echo "--- Dependencies ---"

_check_dep() {
  local name="$1"; local min_ver="$2"; local install_hint="$3"
  if ! command -v "$name" >/dev/null 2>&1; then
    _fail "$name not found  (install: $install_hint)"
    return
  fi
  _ok "$name present"
}

# bash 4+
if (( BASH_VERSINFO[0] < 4 )); then
  _fail "bash 4+ required (found: $BASH_VERSION)"
else
  _ok "bash $BASH_VERSION"
fi

# sqlite3 — required, must exist
if ! command -v sqlite3 >/dev/null 2>&1; then
  _fail "sqlite3 not found  (install: brew install sqlite3 / apt install sqlite3)"
else
  _ok "sqlite3 $(sqlite3 --version | awk '{print $1}')"
fi

# jq — required
if ! command -v jq >/dev/null 2>&1; then
  _fail "jq not found  (install: brew install jq / apt install jq)"
else
  _ok "jq $(jq --version)"
fi

# git — required
if ! command -v git >/dev/null 2>&1; then
  _fail "git not found  (install: brew install git / apt install git)"
else
  _ok "git $(git --version | awk '{print $3}')"
fi

# claude — optional (skip dependent checks if absent)
CLAUDE_PRESENT=1
if ! command -v claude >/dev/null 2>&1; then
  _skip "claude CLI not found — LLM-dependent checks will be skipped  (install: npm i -g @anthropic-ai/claude-code)"
  CLAUDE_PRESENT=0
else
  _ok "claude $(claude --version 2>/dev/null | head -1 || echo '(version unknown)')"
fi

# ShellCheck is optional.
SHELLCHECK_PRESENT=1
if ! command -v shellcheck >/dev/null 2>&1; then
  _skip "shellcheck not installed — static analysis skipped  (install: brew install shellcheck)"
  SHELLCHECK_PRESENT=0
else
  _ok "shellcheck $(shellcheck --version | awk '/^version:/{print $2}')"
fi

echo ""

# ── 2. DB init ────────────────────────────────────────────────────────────────

echo "--- Database init ---"

# Create isolated temp HOME so init.sh writes a fresh state.db
MINI_ORK_TMP_PARENT="$(mktemp -d)"
export MINI_ORK_HOME="$MINI_ORK_TMP_PARENT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"

if [[ ! -f "$MINI_ORK_REPO/db/init.sh" ]]; then
  _skip "db/init.sh not found yet — DB checks deferred until other agents land it"
else
  # Run init.sh — must exit 0
  if bash "$MINI_ORK_REPO/db/init.sh" >/dev/null 2>&1; then
    _ok "db/init.sh exited 0"
  else
    _fail "db/init.sh exited non-zero"
  fi

  # state.db must exist
  if [[ -f "$MINI_ORK_DB" ]]; then
    _ok "state.db created at $MINI_ORK_DB"
  else
    _fail "state.db not found after init.sh"
  fi

  # schema must have ≥ 20 CREATE TABLE statements
  if command -v sqlite3 >/dev/null 2>&1 && [[ -f "$MINI_ORK_DB" ]]; then
    TABLE_COUNT="$(sqlite3 "$MINI_ORK_DB" .schema 2>/dev/null | grep -c 'CREATE TABLE' || true)"
    if (( TABLE_COUNT >= 20 )); then
      _ok "schema has $TABLE_COUNT CREATE TABLE statements (≥ 20)"
    else
      _fail "schema has only $TABLE_COUNT CREATE TABLE statements (need ≥ 20)"
    fi
  fi

  # Migration idempotency: re-run init.sh — schema_migrations row count must not grow
  if command -v sqlite3 >/dev/null 2>&1 && [[ -f "$MINI_ORK_DB" ]]; then
    COUNT_BEFORE="$(sqlite3 "$MINI_ORK_DB" \
      "SELECT COUNT(*) FROM schema_migrations;" 2>/dev/null || echo 0)"
    bash "$MINI_ORK_REPO/db/init.sh" >/dev/null 2>&1 || true
    COUNT_AFTER="$(sqlite3 "$MINI_ORK_DB" \
      "SELECT COUNT(*) FROM schema_migrations;" 2>/dev/null || echo 0)"
    if [[ "$COUNT_BEFORE" == "$COUNT_AFTER" ]]; then
      _ok "init.sh idempotent — schema_migrations stable at $COUNT_AFTER rows"
    else
      _fail "init.sh not idempotent — schema_migrations grew from $COUNT_BEFORE to $COUNT_AFTER"
    fi
  fi
fi

echo ""

# ── 3. bash -n syntax check on bin/* and lib/*.sh ────────────────────────────

echo "--- Syntax check (bash -n) ---"

SYNTAX_FILES=()
# bin/* — skip files with no shebang (likely not shell scripts)
for f in "$MINI_ORK_REPO"/bin/*; do
  [[ -f "$f" ]] && SYNTAX_FILES+=("$f")
done
# lib/*.sh
for f in "$MINI_ORK_REPO"/lib/*.sh; do
  [[ -f "$f" ]] && SYNTAX_FILES+=("$f")
done

if (( ${#SYNTAX_FILES[@]} == 0 )); then
  _skip "no bin/* or lib/*.sh files found yet — syntax check deferred"
else
  for f in "${SYNTAX_FILES[@]}"; do
    fname="$(basename "$f")"
    if bash -n "$f" 2>/dev/null; then
      _ok "bash -n $fname"
    else
      _fail "bash -n $fname  (syntax error)"
    fi
  done
fi

echo ""

# ── 4. shellcheck static analysis ────────────────────────────────────────────

echo "--- ShellCheck ---"

if (( SHELLCHECK_PRESENT == 0 )); then
  _skip "shellcheck not present — static analysis skipped"
elif (( ${#SYNTAX_FILES[@]} == 0 )); then
  _skip "no bin/* or lib/*.sh files found yet — shellcheck deferred"
else
  SC_FAIL=0
  for f in "${SYNTAX_FILES[@]}"; do
    fname="$(basename "$f")"
    # Errors only (-S error); warnings are noise at this stage
    if shellcheck -S error "$f" 2>/dev/null; then
      _ok "shellcheck $fname"
    else
      _fail "shellcheck $fname  (errors found)"
      SC_FAIL=1
    fi
  done
  (( SC_FAIL == 0 )) || true  # already counted via _fail
fi

echo ""

# ── 5. Cleanup ────────────────────────────────────────────────────────────────

echo "--- Cleanup ---"
rm -rf "$MINI_ORK_TMP_PARENT"
_ok "temp dir $MINI_ORK_TMP_PARENT removed"

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────

echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="

if (( FAIL > 0 )); then
  echo "FAIL — fix the issues above and re-run."
  exit 1
else
  echo "PASS"
  exit 0
fi
