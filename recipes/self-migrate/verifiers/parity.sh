#!/usr/bin/env bash
# verifiers/parity.sh — the byte-parity moat for a fork migration.
#
# The un-gameable oracle: the same deterministic entrypoint invocations run
# under both runtimes (MINI_ORK_RUNTIME=bash vs =python via runtime-select) must
# produce identical stdout/stderr/exit-code. Reuses scripts/runtime-parity-harness.sh.
#
# Inputs (env): MINI_ORK_RUN_DIR (required), MINI_ORK_ROOT (repo root),
#               MO_FORK (the fork being migrated, e.g. "verify") — informational.
# Output: JSON to stdout with .pass. Exit code mirrors .pass.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MO_TARGET_CWD:-${MINI_ORK_ROOT:-$(pwd)}}"
FORK="${MO_FORK:-}"
HARNESS="$REPO_ROOT/scripts/runtime-parity-harness.sh"
EVIDENCE="$RUN_DIR/verifier-parity.log"
FORK_TEST="$REPO_ROOT/tests/unit/test_mini_ork_${FORK}_py.py"

pass=true
reasons=()

if [ -n "$FORK" ] && [ -f "$FORK_TEST" ]; then
  if (
    cd "$REPO_ROOT"
    env -u MINI_ORK_RUN_DIR -u MINI_ORK_RECIPE -u MINI_ORK_RUN_ID \
      -u MINI_ORK_PLAN_PATH -u MINI_ORK_TASK_CLASS \
      python3 -m pytest "$FORK_TEST" -q -p no:cacheprovider
  ) >"$EVIDENCE" 2>&1; then
    pass=true
  else
    pass=false; reasons+=("fork parity test failed: $FORK_TEST — see verifier-parity.log")
  fi
elif [ ! -f "$HARNESS" ]; then
  pass=false; reasons+=("no fork parity test and runtime-parity-harness.sh not found at $HARNESS")
else
  if bash "$HARNESS" >"$EVIDENCE" 2>&1; then
    pass=true
  else
    pass=false; reasons+=("cross-runtime parity harness reported a divergence — see verifier-parity.log")
  fi
fi

# JSON emit
python3 - "$pass" "$EVIDENCE" "$FORK" "${reasons[@]:-}" <<'PY'
import json, sys
pass_str, evidence, fork = sys.argv[1], sys.argv[2], sys.argv[3]
reasons = [r for r in sys.argv[4:] if r]
print(json.dumps({
    "name": "parity",
    "fork": fork,
    "pass": pass_str == "true",
    "evidence": evidence,
    "reasons": reasons,
}))
PY

[ "$pass" = true ]
