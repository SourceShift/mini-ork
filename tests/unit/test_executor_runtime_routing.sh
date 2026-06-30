#!/usr/bin/env bash
# tests/unit/test_executor_runtime_routing.sh — R0b: prove bin/mini-ork-execute's
# _run_verifier_ref routes through mo_runtime_exec (default 'local' backend
# preserves byte-identical behavior). Pattern after tests/unit/test_runtime_contract.sh.
#
# Filename ends in .sh (not test_*.py) so pytest's default discovery skips it.
# Run with: bash tests/unit/test_executor_runtime_routing.sh
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
EXECUTOR="$MINI_ORK_ROOT/bin/mini-ork-execute"
CONTRACT="$MINI_ORK_ROOT/lib/runtime/contract.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

cleanup_workspace() {
  if [ -n "${WORKSPACE:-}" ] && [ -d "${WORKSPACE}" ]; then
    rm -rf "${WORKSPACE}"
  fi
}

echo "── unit: bin/mini-ork-execute _run_verifier_ref routing ──"

if [ ! -f "$EXECUTOR" ]; then
  _skip "bin/mini-ork-execute missing"
elif [ ! -f "$CONTRACT" ]; then
  _skip "lib/runtime/contract.sh missing"
else
  WORKSPACE="$(mktemp -d /tmp/mo-executor-routing-XXXXXX)"
  trap cleanup_workspace EXIT

  # Each subtest runs in a fresh bash -c so errexit (inherited from the
  # executor's `set -Eeuo pipefail` after sourcing) is scoped to the child.
  # We disable errexit at the subshell boundary to avoid the outer test
  # harness aborting on `_run_verifier_ref` rc=1 (the failing-verifier path).
  # ── (g) passing verifier routed through _run_verifier_ref returns rc=0 ──
  echo ""
  echo "--- (g) passing verifier: rc=0 + evidence has JSON ---"
  mkdir -p "$WORKSPACE/g"
  cat >"$WORKSPACE/g/verifier.sh" <<'SH'
#!/usr/bin/env bash
echo '{"pass": true, "marker": "r0b-g"}'
SH
  chmod +x "$WORKSPACE/g/verifier.sh"
  (
    set +e
    bash -c '
      set -Eeuo pipefail
      export MO_TARGET_CWD="'"$WORKSPACE"'/g"
      export MINI_ORK_EXECUTE_SOURCE_ONLY=1
      export MINI_ORK_RUN_DIR="'"$WORKSPACE"'"
      # Pre-set per-callsite runtime vars that _run_verifier_ref dereferences
      # under set -u (production callsite at executor:2366 sets these before
      # invoking the function).
      export ARTIFACT_PATH="'"$WORKSPACE"'/g/artifact.json"
      export PLAN_PATH="'"$WORKSPACE"'/plan.json"
      # shellcheck source=/dev/null
      source "'"$EXECUTOR"'"
      OUT="'"$WORKSPACE"'/g/evidence.txt"
      _run_verifier_ref "'"$WORKSPACE"'/g/verifier.sh" "$OUT" >/dev/null 2>&1
      echo "rc=$?"
    '
  ) >"$WORKSPACE/g/run.out" 2>&1
  rc="$(grep '^rc=' "$WORKSPACE/g/run.out" | tail -n1 | cut -d= -f2)"
  OUT="$WORKSPACE/g/evidence.txt"
  if [ "${rc:-X}" = "0" ] && [ -s "$OUT" ] && grep -q '"pass": true' "$OUT" && grep -q 'r0b-g' "$OUT"; then
    _ok "(g) passing verifier returns rc=0 with evidence JSON"
  else
    echo "    run: $(cat "$WORKSPACE/g/run.out" 2>/dev/null)"
    echo "    evidence: $(cat "$OUT" 2>/dev/null)"
    _fail "(g) passing verifier did not return rc=0 with evidence JSON"
  fi

  # ── (h) failing verifier returns rc=1 ───────────────────────────────────
  echo ""
  echo "--- (h) failing verifier: rc=1 ---"
  mkdir -p "$WORKSPACE/h"
  cat >"$WORKSPACE/h/verifier.sh" <<'SH'
#!/usr/bin/env bash
echo '{"pass": false, "marker": "r0b-h"}'
SH
  chmod +x "$WORKSPACE/h/verifier.sh"
  (
    set +e
    bash -c '
      set -Eeuo pipefail
      export MO_TARGET_CWD="'"$WORKSPACE"'/h"
      export MINI_ORK_EXECUTE_SOURCE_ONLY=1
      export MINI_ORK_RUN_DIR="'"$WORKSPACE"'"
      export ARTIFACT_PATH="'"$WORKSPACE"'/h/artifact.json"
      export PLAN_PATH="'"$WORKSPACE"'/plan.json"
      # shellcheck source=/dev/null
      source "'"$EXECUTOR"'"
      OUT="'"$WORKSPACE"'/h/evidence.txt"
      # The verifier prints {\"pass\": false} so _run_verifier_ref returns 1;
      # capture rc without letting set -e abort the bash -c child.
      set +e
      _run_verifier_ref "'"$WORKSPACE"'/h/verifier.sh" "$OUT" >/dev/null 2>&1
      rc=$?
      echo "rc=$rc"
    '
  ) >"$WORKSPACE/h/run.out" 2>&1
  rc="$(grep '^rc=' "$WORKSPACE/h/run.out" | tail -n1 | cut -d= -f2)"
  OUT="$WORKSPACE/h/evidence.txt"
  if [ "${rc:-X}" = "1" ] && [ -s "$OUT" ] && grep -q '"pass": false' "$OUT" && grep -q 'r0b-h' "$OUT"; then
    _ok "(h) failing verifier returns rc=1 with evidence JSON"
  else
    echo "    run: $(cat "$WORKSPACE/h/run.out" 2>/dev/null)"
    echo "    evidence: $(cat "$OUT" 2>/dev/null)"
    _fail "(h) failing verifier did not return rc=1 with evidence JSON"
  fi

  # ── (i) explicit MO_RUNTIME_BACKEND=local does not regress (g) or (h) ──
  # Regression-catcher: a future refactor that bypasses mo_runtime_exec (e.g.
  # reverts to inline subshell) breaks this test rather than silently passing
  # on the un-set-env path. Forcing MO_RUNTIME_BACKEND=local also forces the
  # source-time factory at contract.sh:80 to load 'local' (vs the implicit
  # default), proving the seam is actually being exercised.
  echo ""
  echo "--- (i) explicit MO_RUNTIME_BACKEND=local regression ---"
  mkdir -p "$WORKSPACE/i"
  cat >"$WORKSPACE/i/verifier_pass.sh" <<'SH'
#!/usr/bin/env bash
echo '{"pass": true, "marker": "r0b-i-pass"}'
SH
  chmod +x "$WORKSPACE/i/verifier_pass.sh"
  cat >"$WORKSPACE/i/verifier_fail.sh" <<'SH'
#!/usr/bin/env bash
echo '{"pass": false, "marker": "r0b-i-fail"}'
SH
  chmod +x "$WORKSPACE/i/verifier_fail.sh"
  (
    set +e
    bash -c '
      set -Eeuo pipefail
      export MO_RUNTIME_BACKEND=local
      export MO_TARGET_CWD="'"$WORKSPACE"'/i"
      export MINI_ORK_EXECUTE_SOURCE_ONLY=1
      export MINI_ORK_RUN_DIR="'"$WORKSPACE"'"
      export ARTIFACT_PATH="'"$WORKSPACE"'/i/artifact.json"
      export PLAN_PATH="'"$WORKSPACE"'/plan.json"
      # shellcheck source=/dev/null
      source "'"$EXECUTOR"'"
      # Disable errexit for the rc=1 expected from the failing verifier.
      set +e
      rc_pass=99; rc_fail=99
      OUT1="'"$WORKSPACE"'/i/evidence_pass.txt"
      _run_verifier_ref "'"$WORKSPACE"'/i/verifier_pass.sh" "$OUT1" >/dev/null 2>&1
      rc_pass=$?
      OUT2="'"$WORKSPACE"'/i/evidence_fail.txt"
      _run_verifier_ref "'"$WORKSPACE"'/i/verifier_fail.sh" "$OUT2" >/dev/null 2>&1
      rc_fail=$?
      echo "rc_pass=$rc_pass rc_fail=$rc_fail"
    '
  ) >"$WORKSPACE/i/run.out" 2>&1
  rc_pass="$(grep -oE 'rc_pass=[0-9]+' "$WORKSPACE/i/run.out" | tail -n1 | cut -d= -f2)"
  rc_fail="$(grep -oE 'rc_fail=[0-9]+' "$WORKSPACE/i/run.out" | tail -n1 | cut -d= -f2)"
  OUT1="$WORKSPACE/i/evidence_pass.txt"
  OUT2="$WORKSPACE/i/evidence_fail.txt"
  if [ "${rc_pass:-X}" = "0" ] && [ "${rc_fail:-X}" = "1" ] \
     && grep -q '"pass": true' "$OUT1" && grep -q 'r0b-i-pass' "$OUT1" \
     && grep -q '"pass": false' "$OUT2" && grep -q 'r0b-i-fail' "$OUT2"; then
    _ok "(i) explicit MO_RUNTIME_BACKEND=local does not regress (g) or (h)"
  else
    echo "    run: $(cat "$WORKSPACE/i/run.out" 2>/dev/null)"
    echo "    pass_evidence: $(cat "$OUT1" 2>/dev/null)"
    echo "    fail_evidence: $(cat "$OUT2" 2>/dev/null)"
    _fail "(i) explicit MO_RUNTIME_BACKEND=local regressed (g) or (h)"
  fi
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
