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

if [ -d "$WT/tests/integration" ] && ls "$WT/tests/integration"/test_*.sh >/dev/null 2>&1; then
  for t in "$WT/tests/integration"/test_*.sh; do
    [ -x "$t" ] || continue
    _run_suite "integration:$(basename "$t")" "bash '$t'"
  done
fi

if [ -d "$WT/tests/unit" ] && ls "$WT/tests/unit"/test_*.sh >/dev/null 2>&1; then
  for t in "$WT/tests/unit"/test_*.sh; do
    [ -x "$t" ] || continue
    _run_suite "unit:$(basename "$t")" "bash '$t'"
  done
fi

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
