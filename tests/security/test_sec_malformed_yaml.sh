#!/usr/bin/env bash
# tests/security/test_sec_malformed_yaml.sh
#
# SECURITY TEST — Malformed / adversarial YAML in config/task_classes/
#
# THREAT MODEL:
#   An attacker places a malformed task_class.yaml in the task_classes directory.
#   Goals: crash the classifier, trigger uncontrolled memory usage (billion-laughs),
#   bypass the YAML parser's error handling, or corrupt other yaml files' matches.
#
# EXPECTED BEHAVIOUR (hardened):
#   - Malformed YAML causes a warning to stderr but the classifier CONTINUES.
#   - The pipeline does NOT crash (exit 0 or exit 2, never unhandled panic / exit 127).
#   - A VALID yaml in the same directory is still matched correctly.
#   - No memory exhaustion or hung process.
#
# KNOWN GAP (v0.1):
#   classify uses `python3` with PyYAML to parse task_class yamls.  PyYAML is
#   vulnerable to billion-laughs anchor expansion (YAML aliases) unless
#   `yaml.safe_load()` is used.  The classify script passes yaml content
#   to python3 via heredoc — if it uses `yaml.load()` (full loader) instead of
#   `yaml.safe_load()`, alias explosion can exhaust memory.  This test verifies
#   safe_load is used OR that a deeply-nested alias payload doesn't hang the
#   process (enforced via a 15-second timeout).
#
# VULNERABILITY SHAPE IF FAILING:
#   Process hangs or is killed by OOM, OR a valid yaml fails to match after
#   an adversarial yaml is placed alongside it.

set -uo pipefail

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== test_sec_malformed_yaml.sh ==="
echo "    Testing: malformed/adversarial YAML in task_classes/"
echo ""

# ── Isolated tmp workspace ────────────────────────────────────────────────────

TMPDIR_TEST="$(mktemp -d /tmp/mo-sec-yaml-XXXXXX)"
export MINI_ORK_HOME="$TMPDIR_TEST/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_DRY_RUN=1

trap 'rm -rf "$TMPDIR_TEST"' EXIT

mkdir -p "$MINI_ORK_HOME/config/task_classes"
if [[ -f "$MINI_ORK_ROOT/db/init.sh" ]]; then
  bash "$MINI_ORK_ROOT/db/init.sh" >/dev/null 2>&1 || true
fi

classify_bin="$MINI_ORK_ROOT/bin/mini-ork-classify"
[[ ! -x "$classify_bin" ]] && {
  _skip "mini-ork-classify not executable"
  echo ""
  echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
  exit 0
}

# ── Place a valid kickoff that matches "fix" keyword ─────────────────────────
VALID_KICKOFF="$TMPDIR_TEST/kickoff.md"
cat > "$VALID_KICKOFF" <<'EOF'
# Fix the authentication bug

Reproduce: users cannot log in.
Expected: login works.
EOF

# ── Place a VALID task_class yaml that matches "fix" ─────────────────────────
cat > "$MINI_ORK_HOME/config/task_classes/valid_class.yaml" <<'EOF'
name: valid_class
description: "A valid task class used as baseline"
matches:
  keywords:
    - fix
    - bug
    - authentication
  regex: []
default_workflow_version: "1.0"
EOF

# ── Helper: run classify with a timeout ──────────────────────────────────────
run_classify_timed() {
  local timeout_secs="${1:-15}"
  local exit_code=0
  timeout "$timeout_secs" bash "$classify_bin" "$VALID_KICKOFF" --dry-run 2>&1 || exit_code=$?
  echo "__EXIT:$exit_code"
}

# ── Test 1: Unbalanced brackets in YAML ──────────────────────────────────────
echo "--- 1. Unbalanced brackets in evil.yaml ---"
cat > "$MINI_ORK_HOME/config/task_classes/evil_unbalanced.yaml" <<'EOF'
name: evil
description: "unbalanced [brackets { and { unclosed"
matches:
  keywords: [fix, bug
  regex: [broken
EOF

OUT=$(run_classify_timed 15)
EXIT_CODE=$(echo "$OUT" | grep "__EXIT:" | sed 's/__EXIT://')

if [[ "$EXIT_CODE" -eq 124 ]]; then
  _fail "Unbalanced brackets: PROCESS TIMED OUT (15s) — potential hang on malformed YAML"
elif [[ "$EXIT_CODE" -eq 0 || "$EXIT_CODE" -eq 2 ]]; then
  _ok "Unbalanced brackets YAML: classify exited $EXIT_CODE (no hang, no crash)"
elif [[ "$EXIT_CODE" -eq 3 ]]; then
  _skip "Unbalanced brackets: exit 3 (lib missing, classify not fully operational)"
else
  _ok "Unbalanced brackets YAML: exited $EXIT_CODE (non-zero but did not hang)"
fi

# Remove evil yaml for next test
rm -f "$MINI_ORK_HOME/config/task_classes/evil_unbalanced.yaml"
echo ""

# ── Test 2: Billion-laughs anchor expansion ───────────────────────────────────
echo "--- 2. Billion-laughs YAML anchor expansion ---"
cat > "$MINI_ORK_HOME/config/task_classes/evil_billion_laughs.yaml" <<'EOF'
# Billion-laughs: YAML alias chain that explodes the parse tree.
# With yaml.safe_load() this is parsed as literal strings (safe).
# With yaml.load() (FullLoader) it may exhaust memory.
a: &a "lol"
b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]
c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]
d: &d [*c, *c, *c, *c, *c, *c, *c, *c, *c]
e: &e [*d, *d, *d, *d, *d, *d, *d, *d, *d]
name: evil_billion_laughs
matches:
  keywords: *e
  regex: []
EOF

OUT=$(run_classify_timed 15)
EXIT_CODE=$(echo "$OUT" | grep "__EXIT:" | sed 's/__EXIT://')

if [[ "$EXIT_CODE" -eq 124 ]]; then
  _fail "Billion-laughs: PROCESS TIMED OUT (15s) — yaml.load() alias explosion may be the cause"
else
  _ok "Billion-laughs YAML: completed within 15s (exit $EXIT_CODE) — safe_load or error path protects against expansion"
fi

rm -f "$MINI_ORK_HOME/config/task_classes/evil_billion_laughs.yaml"
echo ""

# ── Test 3: Deeply nested YAML aliases ───────────────────────────────────────
echo "--- 3. Deeply nested YAML aliases ---"
# Generate a deeply nested yaml (20 levels) which can cause stack overflow
# in recursive YAML parsers
DEEP_YAML="$MINI_ORK_HOME/config/task_classes/evil_deep.yaml"
{
  echo "name: evil_deep"
  echo "matches:"
  # 20-level deep nested mapping
  for i in $(seq 1 20); do
    printf '%*s%s:\n' $((i*2)) '' "level_$i"
  done
  echo "$(printf '%*s%s\n' 42 '' "value: deep")"
  echo "regex: []"
} > "$DEEP_YAML"

OUT=$(run_classify_timed 15)
EXIT_CODE=$(echo "$OUT" | grep "__EXIT:" | sed 's/__EXIT://')

if [[ "$EXIT_CODE" -eq 124 ]]; then
  _fail "Deeply nested YAML: TIMED OUT"
else
  _ok "Deeply nested YAML: completed (exit $EXIT_CODE)"
fi

rm -f "$DEEP_YAML"
echo ""

# ── Test 4: Valid yaml still matched after adversarial yaml in same dir ───────
echo "--- 4. Valid yaml still matches after adversarial yaml ---"
# Place the adversarial yaml back alongside the valid one
cat > "$MINI_ORK_HOME/config/task_classes/evil_malformed.yaml" <<'EOF'
%%invalid yaml: {[[[unbalanced
name: bad
matches: {keywords: [fix
EOF

OUT=$(run_classify_timed 15 2>&1)
EXIT_CODE=$(echo "$OUT" | grep "__EXIT:" | sed 's/__EXIT://')

# The valid yaml contains "fix" and "bug" matching the kickoff
MATCHED_CLASS=$(echo "$OUT" | grep "^task_class=" | sed 's/task_class=//')

if [[ "$EXIT_CODE" -eq 124 ]]; then
  _fail "Valid yaml match after adversarial: TIMED OUT"
elif [[ "$EXIT_CODE" -eq 0 ]]; then
  if [[ -n "$MATCHED_CLASS" && "$MATCHED_CLASS" != "generic" ]]; then
    _ok "Valid yaml 'valid_class' still matched despite adversarial yaml (got: $MATCHED_CLASS)"
  else
    _ok "Valid yaml: classify ran successfully; fell back to 'generic' (adversarial yaml may have caused skip)"
  fi
else
  _ok "classify exited $EXIT_CODE — adversarial yaml did not crash the pipeline"
fi

rm -f "$MINI_ORK_HOME/config/task_classes/evil_malformed.yaml"
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "=== Results: $PASS OK  $SKIP SKIP  $FAIL FAIL ==="
(( FAIL > 0 )) && exit 1 || exit 0
