#!/usr/bin/env bash
# verifiers/parity.sh — the byte-parity moat for a fork migration.
#
# Open forks compare both runtimes. Closed forks validate the durable passing
# pre-retirement receipt plus their standalone post-retirement contract.
# Reuses scripts/runtime-parity-harness.sh.
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

if [ -n "$FORK" ] && [ -f "$FORK_TEST" ] && [ -f "$HARNESS" ]; then
  if (
    cd "$REPO_ROOT"
    env -u MINI_ORK_RUN_DIR -u MINI_ORK_RECIPE -u MINI_ORK_RUN_ID \
      -u MINI_ORK_PLAN_PATH -u MINI_ORK_TASK_CLASS \
      MO_PRE_RETIREMENT_REPORT="$RUN_DIR/pre-retirement-parity.json" \
      MO_PRE_RETIREMENT_EVIDENCE="$RUN_DIR/pre-retirement-parity-evidence.log" \
      bash "$HARNESS" "$FORK"
  ) >"$EVIDENCE" 2>&1; then
    pass=true
  else
    pass=false; reasons+=("post-retirement contract failed: $FORK_TEST — see verifier-parity.log")
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
