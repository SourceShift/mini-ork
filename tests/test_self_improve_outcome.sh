#!/usr/bin/env bash
# Regression test for the iter-33 bookkeeping bug.
#
# Bug: when a verifier (e.g. self-tests-pass.sh) failed at execute time
# and never wrote verifier-result-<name>.json, bin/mini-ork-self-improve's
# _read_verifier_inner fell back to running the verifier in DRY-RUN mode
# which always returns pass=true. This let real failures slip through as
# self_improve_runs.outcome=success.
#
# Fix: missing JSON → pass=0; exec_rc != 0 → outcome=rejected with
# diagnostic notes that capture verifier states.
#
# This test exercises the outcome-decision block extracted from
# bin/mini-ork-self-improve against three scenarios.
#
# Run: bash tests/test_self_improve_outcome.sh

set -uo pipefail

REPO_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT="$REPO_ROOT"

# Extract _read_verifier_inner from the current bin/mini-ork-self-improve
# via brace-balancing so this test stays in sync with the real code.
SLICE=$(mktemp)
python3 - "$REPO_ROOT/bin/mini-ork-self-improve" <<'PY' > "$SLICE"
import sys, re
src = open(sys.argv[1]).read()
m = re.search(r'_read_verifier_inner\(\)\s*\{', src)
if not m:
    sys.exit("_read_verifier_inner not found")
i = m.end(); depth = 1
while i < len(src) and depth > 0:
    depth += (src[i] == '{') - (src[i] == '}'); i += 1
print(src[m.start():i])
PY

# Source the extracted function
source "$SLICE"

# Outcome decision (matches the production block exactly)
decide_outcome() {
  local exec_rc="$1"
  local pass_bottle pass_tests pass_reg converged
  read pass_bottle converged < <(_read_verifier_inner bottlenecks-found)
  pass_bottle="${pass_bottle:-0}"; converged="${converged:-0}"
  pass_tests=0; pass_reg=0
  if [ "$pass_bottle" = "1" ] && [ "$converged" != "1" ]; then
    read pass_tests _ < <(_read_verifier_inner self-tests-pass)
    pass_tests="${pass_tests:-0}"
    if [ "$pass_tests" = "1" ]; then
      read pass_reg _ < <(_read_verifier_inner no-regression)
      pass_reg="${pass_reg:-0}"
    fi
  fi
  local outcome="rejected" notes=""
  if [ "$converged" -eq 1 ]; then
    outcome="converged"; notes="scanner-reported-convergence"
  elif [ "$exec_rc" -eq 124 ]; then
    outcome="timed_out"; notes="per-iter-timeout"
  elif [ "$exec_rc" -ne 0 ]; then
    outcome="rejected"; notes="execute-rc=${exec_rc}"
  elif [ "$pass_bottle" = "1" ] && [ "$pass_tests" = "1" ] && [ "$pass_reg" = "1" ]; then
    outcome="success"; notes="all-verifiers-pass"
  elif [ "$pass_bottle" = "1" ] && { [ "$pass_tests" = "0" ] || [ "$pass_reg" = "0" ]; }; then
    outcome="rejected"; notes="patch-failed-verifier"
  else
    outcome="failed"; notes="planner-or-synth-failed"
  fi
  printf '%s\t%s\n' "$outcome" "$notes"
}

assert_eq() {
  local label="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then
    echo "  PASS  $label"
  else
    echo "  FAIL  $label"
    echo "        got:  $got"
    echo "        want: $want"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

FAIL_COUNT=0
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH" "$SLICE"' EXIT

# ── Scenario A: iter-33 bug reproduction ─────────────────────────────────
# self-tests-pass.json MISSING (verifier failed at execute time),
# exec_rc=1 (mini-ork run signaled node failure).
# Pre-fix: outcome=success (BUG). Post-fix: outcome=rejected.
RUN_DIR="$SCRATCH/iter33-repro"; mkdir -p "$RUN_DIR"
echo '{"pass": true,  "converged": false}' > "$RUN_DIR/verifier-result-bottlenecks-found.json"
# self-tests-pass.json deliberately ABSENT
echo '{"pass": true}'                       > "$RUN_DIR/verifier-result-no-regression.json"
read OUTCOME _ < <(decide_outcome 1)
echo "[scenario A] iter-33 repro: missing verifier JSON + exec_rc=1"
assert_eq "outcome != success when execute reported failure" "$OUTCOME" "rejected"

# ── Scenario B: legitimate success ───────────────────────────────────────
RUN_DIR="$SCRATCH/true-success"; mkdir -p "$RUN_DIR"
for v in bottlenecks-found self-tests-pass no-regression; do
  echo '{"pass": true}' > "$RUN_DIR/verifier-result-$v.json"
done
read OUTCOME NOTES < <(decide_outcome 0)
echo "[scenario B] all verifiers pass, exec_rc=0"
assert_eq "outcome is success" "$OUTCOME" "success"
assert_eq "notes is all-verifiers-pass" "$NOTES" "all-verifiers-pass"

# ── Scenario C: verifier wrote pass=false ────────────────────────────────
RUN_DIR="$SCRATCH/explicit-fail"; mkdir -p "$RUN_DIR"
echo '{"pass": true}'  > "$RUN_DIR/verifier-result-bottlenecks-found.json"
echo '{"pass": false}' > "$RUN_DIR/verifier-result-self-tests-pass.json"
echo '{"pass": true}'  > "$RUN_DIR/verifier-result-no-regression.json"
read OUTCOME NOTES < <(decide_outcome 0)
echo "[scenario C] verifier JSON says pass=false"
assert_eq "outcome is rejected" "$OUTCOME" "rejected"
assert_eq "notes flags patch-failed-verifier" "$NOTES" "patch-failed-verifier"

# ── Scenario D: planner/synth failed early ───────────────────────────────
# bottlenecks-found.json missing (synthesizer failed before writing it).
RUN_DIR="$SCRATCH/synth-fail"; mkdir -p "$RUN_DIR"
# no verifier JSONs at all
read OUTCOME NOTES < <(decide_outcome 0)
echo "[scenario D] synth/planner failed early (no JSONs)"
assert_eq "outcome is failed" "$OUTCOME" "failed"

# ── Scenario E: timeout ──────────────────────────────────────────────────
RUN_DIR="$SCRATCH/timeout"; mkdir -p "$RUN_DIR"
read OUTCOME NOTES < <(decide_outcome 124)
echo "[scenario E] mini-ork run timed out (exec_rc=124)"
assert_eq "outcome is timed_out" "$OUTCOME" "timed_out"

echo ""
if [ "$FAIL_COUNT" -eq 0 ]; then
  echo "✓ all scenarios pass"
  exit 0
else
  echo "✗ $FAIL_COUNT scenario(s) failed"
  exit 1
fi
