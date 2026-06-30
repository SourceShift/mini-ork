#!/usr/bin/env bash
# tests/unit/test_runtime_bubblewrap.sh — R2: prove MO_RUNTIME_BACKEND=bubblewrap
# isolates filesystem writes to $WORKSPACE on Linux+bwrap, and falls back to
# local with a one-line WARN on non-Linux / no-bwrap hosts. Same shape as
# tests/unit/test_runtime_contract.sh so reviewers can diff the two.
#
# Filename ends in .sh (not test_*.py) so pytest's default discovery skips it.
# Run with: bash tests/unit/test_runtime_bubblewrap.sh

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
CONTRACT="$MINI_ORK_ROOT/lib/runtime/contract.sh"
BUBBLEWRAP="$MINI_ORK_ROOT/lib/runtime/bubblewrap.sh"
LOCAL="$MINI_ORK_ROOT/lib/runtime/local.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

cleanup_workspace() {
  if [ -n "${WORKSPACE:-}" ] && [ -d "${WORKSPACE}" ]; then
    rm -rf "${WORKSPACE}"
  fi
  if [ -n "${SIBLING:-}" ] && [ -d "${SIBLING}" ]; then
    rm -rf "${SIBLING}"
  fi
}

echo "── unit: lib/runtime/bubblewrap.sh ──"

if [ ! -f "$CONTRACT" ] || [ ! -f "$BUBBLEWRAP" ] || [ ! -f "$LOCAL" ]; then
  _skip "missing contract.sh / bubblewrap.sh / local.sh"
else
  WORKSPACE="$(mktemp -d /tmp/mo-runtime-bubblewrap-XXXXXX)"
  trap cleanup_workspace EXIT

  unset MO_RUNTIME_BACKEND

  # Capability detection at test time, not source time — the test must
  # behave correctly on the macOS dev box (where this author runs) AND
  # on the Linux CI runner where bwrap is genuinely available.
  IS_LINUX=0
  [ "$(uname -s 2>/dev/null)" = "Linux" ] && IS_LINUX=1
  BWRAP_AVAIL=0
  command -v bwrap >/dev/null 2>&1 && BWRAP_AVAIL=1

  # ── (a) inside-WORKSPACE write succeeds (bubblewrap backend) ───────────────
  echo ""
  echo "--- (a) inside-WORKSPACE write under MO_RUNTIME_BACKEND=bubblewrap ---"
  (
    export MO_RUNTIME_BACKEND=bubblewrap
    # shellcheck source=/dev/null
    source "$CONTRACT"
    target="$WORKSPACE/inside_a.txt"
    out="$(mo_runtime_exec "printf x > '$target' && echo inside-wrote" "$WORKSPACE" 2>/dev/null)"
    rc=$?
    if [ "$rc" -eq 0 ] && [ "$out" = "inside-wrote" ] && [ -s "$target" ]; then
      echo "OK"
    else
      echo "FAIL rc=$rc out='$out' target-exists=$([ -s "$target" ] && echo yes || echo no)"
      exit 1
    fi
  ) && _ok "(a) inside-WORKSPACE write succeeds under bubblewrap" \
    || _fail "(a) inside-WORKSPACE write failed under bubblewrap"

  # ── (b) sibling-tempdir write FAILS (isolation assertion, gated) ──────────
  echo ""
  echo "--- (b) sibling-tempdir write blocked by isolation (gated) ---"
  if [ "$IS_LINUX" = "1" ] && [ "$BWRAP_AVAIL" = "1" ]; then
    # Sibling tmpdir genuinely OUTSIDE $WORKSPACE — not a parent, not a
    # subdir. Lexical independence ensures bwrap's bind-mount boundary
    # is what blocks the write, not path-string tricks.
    SIBLING="$(mktemp -d /tmp/mo-runtime-bubblewrap-sibling-XXXXXX)"
    (
      export MO_RUNTIME_BACKEND=bubblewrap
      # shellcheck source=/dev/null
      source "$CONTRACT"
      target="$SIBLING/outside_b.txt"
      out="$(mo_runtime_exec "printf x > '$target' && echo outside-wrote" "$WORKSPACE" 2>/dev/null)"
      rc=$?
      if [ "$rc" -ne 0 ] && [ ! -e "$target" ]; then
        echo "OK"
      else
        echo "FAIL rc=$rc out='$out' target-exists=$([ -e "$target" ] && echo yes || echo no)"
        exit 1
      fi
    ) && _ok "(b) sibling-tempdir write FAILS under bubblewrap (isolation works)" \
      || _fail "(b) sibling-tempdir write did not fail under bubblewrap"
  else
    _skip "(b) sibling-tempdir isolation assertion (non-Linux or bwrap not on PATH)"
  fi

  # ── (b-fallback) on non-Linux or no-bwrap: command runs AND WARN present ──
  # Runs whenever the isolation assertion was _skipped (i.e. when
  # bubblewrap_available would return false). Proves "degrade never fail":
  # the backend must still execute the command, and the WARN line must
  # surface in the captured stderr/stdout.
  echo ""
  echo "--- (b-fallback) command runs + WARN 'falling back to local' on stderr ---"
  if [ "$IS_LINUX" != "1" ] || [ "$BWRAP_AVAIL" != "1" ]; then
    (
      export MO_RUNTIME_BACKEND=bubblewrap
      # shellcheck source=/dev/null
      source "$CONTRACT"
      target="$WORKSPACE/inside_b_fb.txt"
      # 2>&1 so the WARN line (written to stderr by _mo_runtime_bubblewrap_log
      # BEFORE the child runs) lands in the captured $out.
      out="$(mo_runtime_exec "printf y > '$target' && echo fallback-ran" "$WORKSPACE" 2>&1)"
      rc=$?
      if [ "$rc" -eq 0 ] \
         && echo "$out" | grep -q "fallback-ran" \
         && echo "$out" | grep -q "falling back to local" \
         && [ -s "$target" ]; then
        echo "OK"
      else
        echo "FAIL rc=$rc out='$out' target-exists=$([ -s "$target" ] && echo yes || echo no)"
        exit 1
      fi
    ) && _ok "(b-fallback) command ran AND 'falling back to local' WARN emitted" \
      || _fail "(b-fallback) command did not run or WARN did not appear"
  else
    _skip "(b-fallback) already running under real bwrap (no fall-back path to exercise)"
  fi

  # ── (c) control: same in-WORKSPACE write under local backend succeeds ─────
  echo ""
  echo "--- (c) control: inside-WORKSPACE write under MO_RUNTIME_BACKEND=local ---"
  (
    export MO_RUNTIME_BACKEND=local
    # shellcheck source=/dev/null
    source "$CONTRACT"
    target="$WORKSPACE/inside_c.txt"
    out="$(mo_runtime_exec "printf z > '$target' && echo local-wrote" "$WORKSPACE" 2>/dev/null)"
    rc=$?
    if [ "$rc" -eq 0 ] && [ "$out" = "local-wrote" ] && [ -s "$target" ]; then
      echo "OK"
    else
      echo "FAIL rc=$rc out='$out' target-exists=$([ -s "$target" ] && echo yes || echo no)"
      exit 1
    fi
  ) && _ok "(c) control: in-WORKSPACE write succeeds under local" \
    || _fail "(c) control: in-WORKSPACE write failed under local"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1