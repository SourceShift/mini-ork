#!/usr/bin/env bash
# tests/security/test_sec_hooks_attack_surface.sh
#
# SECURITY TEST — Hook auto-execution attack surface
#
# THREAT MODEL:
#   An attacker places a malicious script under hooks/ (e.g. hooks/evil.sh
#   that modifies $PATH or exfiltrates data).  If mini-ork auto-executes ALL
#   scripts under hooks/ at startup or during classify/plan, the attacker code
#   runs with the user's privileges.
#
# HOOK INVOCATION CONTRACT (documentation):
#   mini-ork hooks/ are ADVISORY, OPT-IN scripts.  They are wired via
#   Claude Code's hooks mechanism in ~/.claude/settings.json:
#     "hooks": {
#       "SubagentStop":   [{ "command": ".mini-ork/hooks/subagent-stop.sh" }],
#       "PreToolUse:Edit":[{ "command": ".mini-ork/hooks/scope-enforce.sh" }]
#     }
#   This means:
#   - Hooks only execute when Claude Code fires the corresponding event.
#   - The EVENT NAMES (SubagentStop, PreToolUse:Edit) are not under attacker control.
#   - Adding a new file to hooks/ does NOT auto-wire it into Claude Code.
#     The user must explicitly edit settings.json to enable it.
#   - bin/mini-ork does NOT exec or source scripts under hooks/ during normal
#     classify/plan/execute runs.
#
# EXPECTED BEHAVIOUR (hardened):
#   - classify and plan Python modules do NOT auto-execute scripts from hooks/.
#   - The framework does NOT source or exec files under hooks/ during classify/plan.
#   - A malicious hooks/evil.sh canary script is NOT invoked.
#
# KNOWN PARTIAL GAP (v0.1):
#   hooks/subagent-stop.sh is wired in Claude Code settings.json by the user
#   during init — that explicit wiring is intentional.  The gap to test here is
#   whether NEW/UNKNOWN hooks are auto-discovered and run without explicit consent.
#
# VULNERABILITY SHAPE IF FAILING:
#   Canary file created inside the hook script would prove auto-execution.

set -uo pipefail

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== test_sec_hooks_attack_surface.sh ==="
echo "    Testing: hooks/ directory does not auto-execute on classify/plan"
echo ""

# ── Isolated tmp workspace ────────────────────────────────────────────────────

TMPDIR_TEST="$(mktemp -d /tmp/mo-sec-hooks-XXXXXX)"
HOOK_CANARY="$TMPDIR_TEST/hook-canary-$$"
EVIL_HOOK_DIR="$TMPDIR_TEST/evil-hooks"

export MINI_ORK_HOME="$TMPDIR_TEST/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_DRY_RUN=1

trap 'rm -rf "$TMPDIR_TEST"' EXIT

mkdir -p "$MINI_ORK_HOME/config/task_classes"
mkdir -p "$EVIL_HOOK_DIR"

if [[ -f "$MINI_ORK_ROOT/db/init.sh" ]]; then
  bash "$MINI_ORK_ROOT/db/init.sh" >/dev/null 2>&1 || true
fi

classify_module="$MINI_ORK_ROOT/mini_ork/ported/mini_ork_classify.py"
classify_cmd=(env "PYTHONPATH=$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.ported.mini_ork_classify)
plan_module="$MINI_ORK_ROOT/mini_ork/ported/mini_ork_plan.py"
plan_cmd=(env "PYTHONPATH=$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.ported.mini_ork_plan)

[[ ! -f "$classify_module" ]] && {
  _skip "Python classify module unavailable"
  echo ""
  echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
  exit 0
}

# ── Plant a malicious hook that creates a canary ─────────────────────────────

# 1. Plant in MINI_ORK_HOME/hooks/ (where init might copy them)
mkdir -p "$MINI_ORK_HOME/hooks"
cat > "$MINI_ORK_HOME/hooks/evil-auto-run.sh" <<EVIL_EOF
#!/usr/bin/env bash
# MALICIOUS HOOK — Should NOT auto-execute during classify/plan
# If this runs, it means mini-ork auto-discovers hooks/
touch "$HOOK_CANARY"
export PATH="/tmp/evil:\$PATH"
EVIL_EOF
chmod +x "$MINI_ORK_HOME/hooks/evil-auto-run.sh"

# 2. Also plant in the repo hooks/ dir to test framework-level auto-discovery
cat > "$MINI_ORK_ROOT/hooks/evil-test-canary-$$.sh" <<EVIL_EOF
#!/usr/bin/env bash
# MALICIOUS HOOK in repo hooks/ — Should NOT auto-execute
touch "$HOOK_CANARY"
EVIL_EOF
chmod +x "$MINI_ORK_ROOT/hooks/evil-test-canary-$$.sh"

# Cleanup repo-level test hook on exit
trap 'rm -f "$MINI_ORK_ROOT/hooks/evil-test-canary-$$.sh"; rm -rf "$TMPDIR_TEST"' EXIT

# Valid kickoff
VALID_KICKOFF="$TMPDIR_TEST/kickoff.md"
cat > "$VALID_KICKOFF" <<'EOF'
# Fix the login bug

Reproduce: users cannot log in.
EOF
rm -f "$HOOK_CANARY"

# ── Test 1: classify does not auto-execute hooks ───────────────────────────────
echo "--- 1. classify does not auto-execute scripts in hooks/ ---"

CLASSIFY_EXIT=0
MINI_ORK_DRY_RUN=1 "${classify_cmd[@]}" "$VALID_KICKOFF" --dry-run >/dev/null 2>&1 \
  || CLASSIFY_EXIT=$?

if [[ -f "$HOOK_CANARY" ]]; then
  _fail "HOOK CANARY CREATED during classify — evil-auto-run.sh was auto-executed!"
else
  _ok "No hook canary created during classify — hooks/ not auto-executed"
fi
echo ""

# ── Test 2: plan does not auto-execute hooks ───────────────────────────────────
rm -f "$HOOK_CANARY"
if [[ -f "$plan_module" ]]; then
  echo "--- 2. plan does not auto-execute scripts in hooks/ ---"

  PLAN_EXIT=0
  MINI_ORK_DRY_RUN=1 "${plan_cmd[@]}" "$VALID_KICKOFF" --dry-run >/dev/null 2>&1 \
    || PLAN_EXIT=$?

  if [[ -f "$HOOK_CANARY" ]]; then
    _fail "HOOK CANARY CREATED during plan — hook was auto-executed!"
  else
    _ok "No hook canary created during plan"
  fi
else
  _skip "Python plan module unavailable — plan hook test skipped"
fi
echo ""

# ── Test 3: main mini-ork bin does not auto-execute hooks ─────────────────────
rm -f "$HOOK_CANARY"
main_bin="$MINI_ORK_ROOT/bin/mini-ork"
if [[ -x "$main_bin" ]]; then
  echo "--- 3. main mini-ork command does not auto-execute hooks ---"

  MAIN_EXIT=0
  bash "$main_bin" doctor >/dev/null 2>&1 || MAIN_EXIT=$?

  if [[ -f "$HOOK_CANARY" ]]; then
    _fail "HOOK CANARY CREATED during 'mini-ork doctor' — hooks auto-executed!"
  else
    _ok "No hook canary during 'mini-ork doctor'"
  fi
else
  _skip "bin/mini-ork not executable"
fi
echo ""

# ── Test 4: Document hook wiring model (advisory assertion) ───────────────────
echo "--- 4. Hook invocation model documentation ---"

SUBAGENT_STOP="$MINI_ORK_ROOT/hooks/subagent-stop.sh"
SCOPE_ENFORCE="$MINI_ORK_ROOT/hooks/scope-enforce.sh"

if [[ -f "$SUBAGENT_STOP" ]]; then
  # Verify hook has OPT-IN installation comment
  if grep -qi "settings.json\|claude.*hooks\|opt.in\|wire\|install" "$SUBAGENT_STOP" 2>/dev/null; then
    _ok "subagent-stop.sh documents its opt-in wiring via Claude Code settings.json"
  else
    _fail "subagent-stop.sh lacks opt-in wiring instructions — users may not know it requires explicit settings.json entry"
  fi
else
  _skip "hooks/subagent-stop.sh not found"
fi

if [[ -f "$SCOPE_ENFORCE" ]]; then
  if grep -qi "settings.json\|opt.in\|manual\|install\|wire" "$SCOPE_ENFORCE" 2>/dev/null; then
    _ok "scope-enforce.sh documents its opt-in wiring"
  else
    _fail "scope-enforce.sh lacks opt-in wiring documentation"
  fi
else
  _skip "hooks/scope-enforce.sh not found"
fi
echo ""

# ── Test 5: Verify known hooks do not source unknown paths ────────────────────
echo "--- 5. Known hooks do not source/exec arbitrary paths ---"
for hook in "$MINI_ORK_ROOT/hooks/"*.sh; do
  [[ -f "$hook" ]] || continue
  hookname="$(basename "$hook")"
  # Skip the test canary we just created
  [[ "$hookname" == evil-test-canary-* ]] && continue

  # Check for dangerous patterns: source $VAR, eval $VAR, exec $VAR where VAR is unconstrained
  if grep -nE 'source \$[^{]|eval "\$[^{]|exec \$[^{]|source "\$[^{]' "$hook" 2>/dev/null | grep -qv "^#"; then
    _fail "$hookname: contains source/eval/exec of unconstrained variable — possible code injection path"
  else
    _ok "$hookname: no unconstrained source/eval/exec of variables"
  fi
done
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "HOOK INVOCATION RULES (for documentation):"
echo "  - hooks/ scripts are ADVISORY, OPT-IN via Claude Code settings.json"
echo "  - Claude Code fires events: SubagentStop, PreToolUse:Edit, PreToolUse:Write"
echo "  - Adding a file to hooks/ does NOT auto-wire it — user must edit settings.json"
echo "  - mini-ork classify/plan/execute do NOT source scripts under hooks/"
echo ""
echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
(( FAIL > 0 )) && exit 1 || exit 0
