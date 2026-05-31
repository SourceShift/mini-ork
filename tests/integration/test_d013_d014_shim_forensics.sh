#!/usr/bin/env bash
# D-013/D-014 regression: shim must preserve forensics on failure +
# surface claude CLI stderr to caller.
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

TMPROOT=$(mktemp -d /tmp/ork-d013-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
export MINI_ORK_HOME="$TMPROOT/.mini-ork"
export MINI_ORK_DB="$MINI_ORK_HOME/state.db"
export MINI_ORK_RUN_ID="test-d013-$$"
mkdir -p "$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID"

PASS=0; FAIL=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

echo "── d-013/d-014: shim forensics + stderr surfacing ──"

# Stub mo_llm_dispatch to always fail + emit a known stderr line.
# (Lib has `set -euo pipefail`; immediately disable so expected
# failures don't kill the test.)
source "$MINI_ORK_ROOT/lib/llm-dispatch.sh"
set +e
mo_llm_dispatch() {
  local out_file="$3"
  echo "stub claude error: rate limit reached" >&2
  echo "(partial output before error)" > "$out_file"
  return 1
}

# Call shim; capture both stdout + stderr
SHIM_OUT=$({ llm_dispatch --node-type planner --prompt-text "trigger failure"; } 2>&1)
RC=$?

# D-013 assertion: forensic dir exists with a preserved .out file
FORENSIC_DIR="$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID/llm-failures"
if [ -d "$FORENSIC_DIR" ] && ls "$FORENSIC_DIR"/*.out >/dev/null 2>&1; then
  _ok "forensic .out preserved at $FORENSIC_DIR"
else
  _fail "no forensic .out preserved (D-013 regression)"
fi

# D-014 assertion: shim emitted the stub's stderr to caller
if echo "$SHIM_OUT" | grep -q "rate limit reached"; then
  _ok "claude stderr surfaced to caller"
else
  _fail "claude stderr NOT surfaced (D-014 regression). Got: $SHIM_OUT"
fi

# D-014 assertion: shim emitted the rc line
if echo "$SHIM_OUT" | grep -qE "llm_dispatch FAIL.*rc=1"; then
  _ok "shim emitted rc + model identifier"
else
  _fail "shim FAIL header missing (D-014 regression). Got: $SHIM_OUT"
fi

# Exit code propagates
if [ "$RC" -eq 1 ]; then
  _ok "shim propagated mo_llm_dispatch exit code"
else
  _fail "shim returned rc=$RC, expected 1"
fi

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
