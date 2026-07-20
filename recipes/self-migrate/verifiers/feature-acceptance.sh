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
# Output: JSON to stdout with .pass. Exit code mirrors .pass.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MO_TARGET_CWD:-${MINI_ORK_ROOT:-$(pwd)}}"
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

# (b) the fork's Python unit contracts
TESTF="$REPO_ROOT/tests/unit/test_mini_ork_${FORK}_py.py"
if [ -n "$FORK" ] && [ -f "$TESTF" ]; then
  if (
    cd "$REPO_ROOT"
    env -u MINI_ORK_RUN_DIR -u MINI_ORK_RECIPE -u MINI_ORK_RUN_ID \
      -u MINI_ORK_PLAN_PATH -u MINI_ORK_TASK_CLASS \
      python3 -m pytest "$TESTF" -q -p no:cacheprovider
  ) >>"$EVIDENCE" 2>&1; then
    echo "[pytest] $TESTF PASS" >>"$EVIDENCE"
  else
    pass=false; reasons+=("pytest $TESTF failed")
  fi
fi

# Reflect has additional inbound contracts beyond its focused unit module:
# GEPA's default path and the standalone CLI integration suite.
if [ "$FORK" = "reflect" ]; then
  if (
    cd "$REPO_ROOT"
    python3 -m pytest tests/test_gepa_wiring_py.py -q -p no:cacheprovider
  ) >>"$EVIDENCE" 2>&1; then
    echo "[pytest] tests/test_gepa_wiring_py.py PASS" >>"$EVIDENCE"
  else
    pass=false; reasons+=("pytest tests/test_gepa_wiring_py.py failed")
  fi
  if ( cd "$REPO_ROOT" && bash tests/integration/test_bin_reflect.sh ) >>"$EVIDENCE" 2>&1; then
    echo "[integration] tests/integration/test_bin_reflect.sh PASS" >>"$EVIDENCE"
  else
    pass=false; reasons+=("reflect integration suite failed")
  fi
fi

# Classify has a broad inbound surface: shell integration callers plus the
# hostile-input contracts that protect its kickoff and environment boundary.
if [ "$FORK" = "classify" ]; then
  if ( cd "$REPO_ROOT" && bash tests/integration/test_bin_classify.sh ) >>"$EVIDENCE" 2>&1; then
    echo "[integration] tests/integration/test_bin_classify.sh PASS" >>"$EVIDENCE"
  else
    pass=false; reasons+=("classify integration suite failed")
  fi
  SECURITY_TESTS=(
    tests/security/test_sec_env_var_pollution.sh
    tests/security/test_sec_hooks_attack_surface.sh
    tests/security/test_sec_kickoff_command_injection.sh
    tests/security/test_sec_kickoff_path_traversal.sh
    tests/security/test_sec_malformed_yaml.sh
    tests/security/test_sec_oversized_input.sh
    tests/security/test_sec_sql_injection_run_id.sh
  )
  for security_test in "${SECURITY_TESTS[@]}"; do
    if ( cd "$REPO_ROOT" && bash "$security_test" ) >>"$EVIDENCE" 2>&1; then
      echo "[security] $security_test PASS" >>"$EVIDENCE"
    else
      pass=false; reasons+=("$security_test failed")
    fi
  done
fi

# Plan retirement has several executable callers whose contracts are broader
# than the focused unit module: module-level CLI behavior, given-plan bypass,
# recipe dry-runs, hostile kickoff input, and the web provenance surface.
if [ "$FORK" = "plan" ]; then
  PLAN_TESTS=(
    tests/integration/test_bin_plan.sh
    tests/integration/test_given_plan.sh
    tests/e2e/test_e2e_recipe_bdd_first.sh
    tests/e2e/test_e2e_recipe_code_fix.sh
    tests/security/test_sec_hooks_attack_surface.sh
    tests/security/test_sec_kickoff_command_injection.sh
    tests/security/test_sec_oversized_input.sh
  )
  for plan_test in "${PLAN_TESTS[@]}"; do
    if ( cd "$REPO_ROOT" && bash "$plan_test" ) >>"$EVIDENCE" 2>&1; then
      echo "[plan-contract] $plan_test PASS" >>"$EVIDENCE"
    else
      pass=false; reasons+=("$plan_test failed")
    fi
  done
  if (
    cd "$REPO_ROOT"
    python3 -m pytest tests/test_web_smoke.py -q -p no:cacheprovider
  ) >>"$EVIDENCE" 2>&1; then
    echo "[pytest] tests/test_web_smoke.py PASS" >>"$EVIDENCE"
  else
    pass=false; reasons+=("tests/test_web_smoke.py failed")
  fi
fi

# (c) type-check the migrated port and the Python callers changed by the rewire.
TYPE_TARGETS=("mini_ork/ported/mini_ork_${FORK}.py")
if [ "$FORK" = "reflect" ]; then
  TYPE_TARGETS+=("mini_ork/ported/mini_ork_cli.py" "mini_ork/ported/mini_ork_execute.py")
fi
if [ "$FORK" = "classify" ]; then
  TYPE_TARGETS+=("mini_ork/ported/mini_ork_cli.py" "mini_ork/web/routes/run_detail.py")
fi
if [ "$FORK" = "plan" ]; then
  TYPE_TARGETS+=("mini_ork/ported/mini_ork_cli.py")
fi
if [ -n "$FORK" ] && [ -f "$REPO_ROOT/${TYPE_TARGETS[0]}" ]; then
  if ( cd "$REPO_ROOT" && python3 -m pyright "${TYPE_TARGETS[@]}" ) >>"$EVIDENCE" 2>&1; then
    echo "[pyright] ${TYPE_TARGETS[*]} 0 errors" >>"$EVIDENCE"
  else
    pass=false; reasons+=("pyright on ${TYPE_TARGETS[*]} not clean")
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

[ "$pass" = true ]
