#!/usr/bin/env bash
# verifiers/self-tests-pass.sh — run mini-ork's own test suite inside
# the worktree the implementer patched. If any test fails, the patch
# is rejected and the runner routes to rollback.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR        run directory
#   MINI_ORK_SELF_IMPROVE_WORKTREE  worktree path (set by outer runner)
#
# Output: JSON. Exit 0 always (caller reads .pass).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
WT="${MINI_ORK_SELF_IMPROVE_WORKTREE:-$MINI_ORK_ROOT}"
EVIDENCE="$RUN_DIR/verifier-self-tests-pass.log"
exec 3>"$EVIDENCE"

cd "$WT" || { echo "worktree missing: $WT" >&3; }

# Run mini-ork's own tests in DRY_RUN mode — most recipe integration
# tests honor this flag and skip live LLM dispatch. Without it iter 1
# burned ~20 min walking the full live-mode suite against an
# un-patched worktree.
export MINI_ORK_DRY_RUN=1

# Coverage scope. Default: unit tests + the recipes' own integration
# smoke tests (the ones the self-improve loop is most likely to break).
# Operator override: MINI_ORK_SELF_IMPROVE_TEST_GLOBS as a space-
# separated glob list relative to the worktree root.
#
# Wider coverage (full integration sweep) is intentionally OFF here
# because the same gauntlet runs in CI on the merged branch — running
# it per-iter costs ~20 min of wall-clock without catching anything
# the per-patch unit + smoke pair misses.
DEFAULT_GLOBS=(
  "tests/unit/test_*.sh"
  "tests/integration/test_recursive_self_improve_recipe.sh"
  "tests/integration/test_post_mvp_delivery_recipe.sh"
  "tests/integration/test_bin_execute.sh"
  "tests/integration/test_d008_workflow_node_dag.sh"
)
read -r -a GLOBS <<< "${MINI_ORK_SELF_IMPROVE_TEST_GLOBS:-${DEFAULT_GLOBS[*]}}"

# We deliberately reject vacuous-pass cases (e.g. zero tests).
ran=0
failed=0
suites=()

_run_suite() {
  local name="$1" cmd="$2"
  echo "===== $name =====" >&3
  if eval "$cmd" >&3 2>&1; then
    suites+=("$name:PASS")
  else
    failed=$((failed+1))
    suites+=("$name:FAIL")
  fi
  ran=$((ran+1))
}

for glob in "${GLOBS[@]}"; do
  for t in $WT/$glob; do
    [ -f "$t" ] || continue
    [ -x "$t" ] || chmod +x "$t" 2>/dev/null
    label="$(basename "$(dirname "$t")"):$(basename "$t")"
    _run_suite "$label" "bash '$t'"
  done
done

if [ "$ran" -eq 0 ]; then
  echo "no test suites found — refusing vacuous pass" >&3
  pass=0
else
  pass=1
  [ "$failed" -gt 0 ] && pass=0
fi

python3 - "$pass" "$ran" "$failed" "$EVIDENCE" "${suites[@]}" <<'PY'
import json, sys
pass_, ran, failed, ev, *suites = sys.argv[1:]
print(json.dumps({
    "verifier": "self-tests-pass",
    "pass": pass_ == "1",
    "evidence_path": ev,
    "suites_run": int(ran),
    "suites_failed": int(failed),
    "suites": suites,
}))
PY
