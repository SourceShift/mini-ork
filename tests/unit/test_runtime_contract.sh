#!/usr/bin/env bash
# tests/unit/test_runtime_contract.sh — unit tests for lib/runtime/contract.sh
#
# Filename ends in .sh (not test_*.py) so pytest's default discovery skips it.
# Run with: bash tests/unit/test_runtime_contract.sh
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
CONTRACT="$MINI_ORK_ROOT/lib/runtime/contract.sh"
LOCAL="$MINI_ORK_ROOT/lib/runtime/local.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

cleanup_workspace() {
  if [ -n "${WORKSPACE:-}" ] && [ -d "${WORKSPACE}" ]; then
    rm -rf "${WORKSPACE}"
  fi
}

echo "── unit: lib/runtime/contract.sh ──"

if [ ! -f "$CONTRACT" ]; then _skip "lib/runtime/contract.sh missing"
elif [ ! -f "$LOCAL" ];   then _skip "lib/runtime/local.sh missing"
else
  WORKSPACE="$(mktemp -d /tmp/mo-runtime-contract-XXXXXX)"
  trap cleanup_workspace EXIT

  # Ensure starting from a clean backend binding for every test.
  unset MO_RUNTIME_BACKEND

  # ── (a) exec success returns rc=0 + correct stdout ────────────────────────
  echo ""
  echo "--- (a) exec success: echo hello ---"
  (
    unset MO_RUNTIME_BACKEND
    # shellcheck source=/dev/null
    source "$CONTRACT"
    out="$(mo_runtime_exec 'echo hello' 2>/dev/null)"
    rc=$?
    if [ "$rc" -eq 0 ] && [ "$out" = "hello" ]; then
      echo "OK"
    else
      echo "FAIL rc=$rc out=$out"; exit 1
    fi
  ) && _ok "(a) exec success returns rc=0 + stdout 'hello'" \
    || _fail "(a) exec success returned non-zero or wrong stdout"

  # ── (b) exec failure returns rc != 0 + empty stdout ───────────────────────
  echo ""
  echo "--- (b) exec failure: false ---"
  (
    unset MO_RUNTIME_BACKEND
    # shellcheck source=/dev/null
    source "$CONTRACT"
    out="$(mo_runtime_exec 'false' 2>/dev/null)"
    rc=$?
    if [ "$rc" -ne 0 ] && [ -z "$out" ]; then
      echo "OK"
    else
      echo "FAIL rc=$rc out='$out'"; exit 1
    fi
  ) && _ok "(b) exec failure returns rc != 0 with empty stdout" \
    || _fail "(b) exec failure did not propagate non-zero rc"

  # ── (c) cwd is honored ────────────────────────────────────────────────────
  echo ""
  echo "--- (c) cwd honored: pwd under \$WORKSPACE ---"
  (
    unset MO_RUNTIME_BACKEND
    # shellcheck source=/dev/null
    source "$CONTRACT"
    out="$(mo_runtime_exec 'pwd' "$WORKSPACE" 2>/dev/null)"
    rc=$?
    # bash's `pwd` defaults to logical mode (-L), so the child returns the
    # path bash was given — even if /tmp is a symlink to /private/tmp.
    # Compare via the same logical chdir-and-print so we don't penalize
    # bash for NOT auto-canonicalizing (that's a pwd -P feature, not the
    # cwd contract's job).
    expected="$(cd "$WORKSPACE" >/dev/null 2>&1 && pwd)"
    if [ "$rc" -eq 0 ] && [ "$out" = "$expected" ]; then
      echo "OK"
    else
      echo "FAIL rc=$rc out='$out' expected='$expected'"; exit 1
    fi
  ) && _ok "(c) cwd honored: pwd returned workspace path" \
    || _fail "(c) cwd not honored"

  # ── (d) timeout kills the whole pgid (marker-stops-growing check) ──────────
  echo ""
  echo "--- (d) timeout kills whole pgid ---"
  (
    unset MO_RUNTIME_BACKEND
    # shellcheck source=/dev/null
    source "$CONTRACT"
    MARKER="${WORKSPACE}/marker_d.txt"
    # Loop writes one byte per iter; sleep 0.05 cadence means > 40 iters/sec
    # WITHOUT timeout. With timeout=0.3, the loop is reaped well inside the
    # 200-iter ceiling, so the assertion has signal.
    cmd="i=0; while [ \$i -lt 200 ]; do printf x >> '$MARKER'; i=\$((i+1)); sleep 0.05; done"
    out="$(mo_runtime_exec "$cmd" "$WORKSPACE" 0.3 2>/dev/null)"
    rc=$?
    size1=0
    [ -f "$MARKER" ] && size1="$(wc -c <"$MARKER" | tr -d ' ')"
    sleep 1
    size2=0
    [ -f "$MARKER" ] && size2="$(wc -c <"$MARKER" | tr -d ' ')"
    if [ "$rc" -eq 124 ] && [ "$size1" -gt 0 ] && [ "$size1" = "$size2" ]; then
      echo "OK"
    else
      echo "FAIL rc=$rc size1=$size1 size2=$size2"; exit 1
    fi
  ) && _ok "(d) timeout kills whole pgid (rc=124, marker size stable)" \
    || _fail "(d) timeout did not stop pgid cleanly"

  # ── (e) factory defaults to local when MO_RUNTIME_BACKEND unset ───────────
  echo ""
  echo "--- (e) factory defaults to local ---"
  (
    unset MO_RUNTIME_BACKEND
    # shellcheck source=/dev/null
    source "$CONTRACT"
    # `mo_runtime_exec` must be callable; if factory loaded 'bogus' the
    # forwarder would have bombed at sourcing time. If factory never ran, the
    # symbol wouldn't exist either.
    if declare -F mo_runtime_exec >/dev/null \
       && declare -F mo_runtime_local_exec >/dev/null; then
      # Functional confirmation: actually invoke the contract.
      out="$(mo_runtime_exec 'echo contract-defaults-local' 2>/dev/null)"
      [ "$out" = "contract-defaults-local" ] || { echo "FAIL out=$out"; exit 1; }
      echo "OK"
    else
      echo "FAIL forwarders/backends not loaded"; exit 1
    fi
  ) && _ok "(e) factory defaults to 'local' when env unset" \
    || _fail "(e) factory did not default to local"

  # ── (f) factory errors clearly on bogus backend ────────────────────────────
  echo ""
  echo "--- (f) factory errors clearly on bogus backend ---"
  bogus_log="${WORKSPACE}/bogus.log"
  (
    export MO_RUNTIME_BACKEND=bogus
    # Capture both rc and stderr without polluting the calling shell.
    set +u
    # shellcheck source=/dev/null
    source "$CONTRACT" 2>"$bogus_log"
    rc=$?
    set -u
    if [ "$rc" -ne 0 ] && grep -q "bogus" "$bogus_log" 2>/dev/null; then
      echo "OK rc=$rc"
    else
      echo "FAIL rc=$rc log=$(cat "$bogus_log" 2>/dev/null)"; exit 1
    fi
  ) && _ok "(f) factory errors clearly on bogus backend (rc!=0, stderr mentions 'bogus')" \
    || _fail "(f) factory did not error clearly on bogus backend"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
