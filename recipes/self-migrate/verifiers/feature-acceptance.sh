#!/usr/bin/env bash
# verifiers/feature-acceptance.sh — the end-to-end feature gate for a fork.
#
# Unit-parity is necessary but not sufficient (a rewire can pass unit-parity yet
# break the feature — e.g. leak stdout). This runs (a) the fork's feature-
# acceptance probe from gates/feature_acceptance.sh and (b) the fork's Python
# test module + pyright, so a green here means the FEATURE works, not just a fn.
#
# Inputs (env): MINI_ORK_RUN_DIR (required), MINI_ORK_ROOT (repo root),
#               MO_FORK (the fork/feature, e.g. "verify").
# Output: JSON to stdout with .pass. Exit 0 always.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MINI_ORK_ROOT:-$(pwd)}"
FORK="${MO_FORK:-}"
GATE="$REPO_ROOT/gates/feature_acceptance.sh"
EVIDENCE="$RUN_DIR/verifier-feature-acceptance.log"
: >"$EVIDENCE"

pass=true
reasons=()

# (a) the feature-acceptance probe for this fork's feature
if [ -n "$FORK" ] && [ -x "$GATE" ]; then
  if MO_FORK="$FORK" bash "$GATE" "$FORK" >>"$EVIDENCE" 2>&1; then
    echo "[feature-probe] $FORK PASS" >>"$EVIDENCE"
  else
    pass=false; reasons+=("feature-acceptance probe for '$FORK' failed")
  fi
elif [ -z "$FORK" ]; then
  reasons+=("MO_FORK not set — cannot select a feature probe (shape-only)")
fi

# (b) the fork's Python test module + pyright on its port
TESTF="$REPO_ROOT/tests/unit/test_mini_ork_${FORK}_py.py"
if [ -n "$FORK" ] && [ -f "$TESTF" ]; then
  if ( cd "$REPO_ROOT" && python3 -m pytest "$TESTF" -q -p no:cacheprovider ) >>"$EVIDENCE" 2>&1; then
    echo "[pytest] $TESTF PASS" >>"$EVIDENCE"
  else
    pass=false; reasons+=("pytest $TESTF failed")
  fi
fi
PORTF="$REPO_ROOT/mini_ork/ported/mini_ork_${FORK}.py"
if [ -n "$FORK" ] && [ -f "$PORTF" ]; then
  if ( cd "$REPO_ROOT" && python3 -m pyright "$PORTF" 2>&1 | grep -q '0 errors' ) ; then
    echo "[pyright] $PORTF 0 errors" >>"$EVIDENCE"
  else
    pass=false; reasons+=("pyright on $PORTF not clean")
  fi
fi

python3 - "$pass" "$EVIDENCE" "$FORK" "${reasons[@]:-}" <<'PY'
import json, sys
pass_str, evidence, fork = sys.argv[1], sys.argv[2], sys.argv[3]
reasons = [r for r in sys.argv[4:] if r]
print(json.dumps({
    "name": "feature-acceptance",
    "fork": fork,
    "pass": pass_str == "true",
    "evidence": evidence,
    "reasons": reasons,
}))
PY
