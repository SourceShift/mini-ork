#!/usr/bin/env bash
# tests/unit/test_scaffold_tier.sh — R5b unit tests.
#
# Covers:
#   1. lib/scaffold_tier.sh resolver (full logic, in isolation).
#   2. The minimal-implementer GLUE the executor runs (run_minimal -> impl log),
#      with a stubbed model — robust, no full-executor source.
#   3. A structural gate assertion: the bin/mini-ork-execute minimal branch is
#      guarded by `[ "$_scaffold_tier" = "minimal" ]` (resolver default=harness),
#      so the default path is byte-identical to pre-R5b.
#
# Why not a full _dispatch_node integration probe: _dispatch_node is defined at
# ~line 1817, AFTER the MINI_ORK_EXECUTE_SOURCE_ONLY=1 return (line 657) and a
# large block of DB/init side-effects, so it cannot be sourced in isolation the
# way _run_verifier_ref (defined at line 74) can. The full branch is exercised by
# real recipe runs; the glue + gate are unit-covered here.
#
# Run with: bash tests/unit/test_scaffold_tier.sh
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
RESOLVER="$MINI_ORK_ROOT/lib/scaffold_tier.sh"
EXECUTOR="$MINI_ORK_ROOT/bin/mini-ork-execute"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }

# Run the resolver in a clean env and echo what it produced.
_resolve() {
  (
    unset MO_SCAFFOLD_TIER MO_NODE_SCAFFOLD
    for kv in "$@"; do
      case "$kv" in
        MO_SCAFFOLD_TIER=*) export "$kv" ;;
        MO_NODE_SCAFFOLD=*) export "$kv" ;;
      esac
    done
    # shellcheck source=/dev/null
    source "$RESOLVER"
    mo_scaffold_tier implementer code_fix
  )
}

echo "── unit: lib/scaffold_tier.sh resolver ──"

if [ ! -f "$RESOLVER" ]; then
  _fail "lib/scaffold_tier.sh missing"
else
  echo ""; echo "--- (a) unset env → harness ---"
  [ "$(_resolve)" = "harness" ] && _ok "(a) unset → harness" || _fail "(a) unset (got: $(_resolve))"

  echo ""; echo "--- (b) MO_SCAFFOLD_TIER=minimal → minimal ---"
  [ "$(_resolve MO_SCAFFOLD_TIER=minimal)" = "minimal" ] && _ok "(b) global minimal" || _fail "(b)"

  echo ""; echo "--- (c) MO_SCAFFOLD_TIER=harness → harness ---"
  [ "$(_resolve MO_SCAFFOLD_TIER=harness)" = "harness" ] && _ok "(c) global harness" || _fail "(c)"

  echo ""; echo "--- (d) MO_NODE_SCAFFOLD=minimal → minimal ---"
  [ "$(_resolve MO_NODE_SCAFFOLD=minimal)" = "minimal" ] && _ok "(d) node minimal" || _fail "(d)"

  echo ""; echo "--- (e) both unset → harness ---"
  [ "$(_resolve)" = "harness" ] && _ok "(e) both unset → harness" || _fail "(e)"

  echo ""; echo "--- (f) global harness overrides node minimal ---"
  [ "$(_resolve MO_SCAFFOLD_TIER=harness MO_NODE_SCAFFOLD=minimal)" = "harness" ] \
    && _ok "(f) global harness wins" || _fail "(f)"

  echo ""; echo "--- (g) garbage MO_SCAFFOLD_TIER → harness ---"
  [ "$(_resolve MO_SCAFFOLD_TIER=mini)" = "harness" ] && _ok "(g) unknown → harness" || _fail "(g)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# (h) minimal-implementer GLUE: the exact contract bin/mini-ork-execute runs in
# its minimal branch — import run_minimal, call it, write impl-<node>.log,
# require non-empty — with dispatch_model stubbed so no real LLM is hit.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "── unit: minimal-implementer glue (run_minimal → impl log) ──"
echo ""
if ! command -v python3 >/dev/null 2>&1; then
  _skip "(h) python3 unavailable"
else
  WS="$(mktemp -d /tmp/mo-scaffold-glue-XXXXXX)"
  trap 'rm -rf "$WS"' EXIT
  IMPL_LOG="$WS/impl-probe.log"
  MINI_ORK_ROOT_VAL="$MINI_ORK_ROOT" IMPL_LOG_VAL="$IMPL_LOG" CWD_VAL="$WS" \
  python3 - <<'PY' >"$WS/glue.out" 2>&1
import os, sys, types
root = os.environ["MINI_ORK_ROOT_VAL"]
sys.path.insert(0, root)
# Stub dispatch_model so the loop completes deterministically without an LLM.
import mini_ork.agent.minimal as m
class _Resp:
    text = "```bash\necho probe > made.txt\n```"
class _Done:
    text = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT MINIMAL_GLUE_OK"
_seq = iter([_Resp(), _Done()])
m.dispatch_model = lambda req, *a, **k: next(_seq)
# Mirror the executor's minimal-branch glue (bin/mini-ork-execute ~2188-2198).
from mini_ork.agent.minimal import run_minimal
res = run_minimal("probe task", cwd=os.environ["CWD_VAL"])
with open(os.environ["IMPL_LOG_VAL"], "w", encoding="utf-8") as fh:
    fh.write(res.final_output or "")
print(f"status={res.exit_status} out_len={len(res.final_output or '')}")
PY
  if [ -s "$IMPL_LOG" ] && grep -q 'MINIMAL_GLUE_OK' "$IMPL_LOG" && grep -q 'status=' "$WS/glue.out"; then
    _ok "(h) run_minimal glue writes non-empty impl log with final_output"
  else
    echo "    glue.out: $(cat "$WS/glue.out" 2>/dev/null)"; echo "    impl: $(cat "$IMPL_LOG" 2>/dev/null)"
    _fail "(h) minimal-implementer glue did not produce expected impl log"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# (i) GATE: the executor's run_minimal branch is guarded by the resolver result
# being "minimal", so the default (harness) path is unreachable unless opted in.
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "── structural: minimal branch gated by mo_scaffold_tier ──"
echo ""
if [ -f "$EXECUTOR" ]; then
  if grep -q 'mo_scaffold_tier' "$EXECUTOR" \
     && grep -q '_scaffold_tier" = "minimal"' "$EXECUTOR" \
     && grep -q 'run_minimal' "$EXECUTOR"; then
    _ok "(i) executor resolves mo_scaffold_tier and gates run_minimal behind =minimal"
  else
    _fail "(i) executor minimal branch not gated by resolver as expected"
  fi
else
  _skip "(i) bin/mini-ork-execute missing"
fi

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
