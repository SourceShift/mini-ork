#!/usr/bin/env bash
# runtime-parity-harness.sh — validate the bash→Python runtime cutover.
#
# Runs the same DETERMINISTIC entrypoint invocations under both runtimes
# (MINI_ORK_RUNTIME=bash vs =python via lib/runtime-select.sh) and diffs the
# outputs. This is the gate that must pass before flipping the default runtime
# to python. It does NOT exercise live LLM dispatch (that needs the real-provider
# integration harness); it validates the deterministic surface end-to-end through
# the actual bin/ shim, complementing the per-module parity unit tests.
#
# Usage: bash scripts/runtime-parity-harness.sh
# Exit 0 = every check identical across runtimes; 1 = a divergence.
set -uo pipefail

ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT="$ROOT"
BIN="$ROOT/bin"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
FAILS=0

# normalize volatile bits (tmp paths, run ids, timestamps) so only real
# behavioral differences surface.
_norm() { sed -E "s#$TMP[^ ]*#<TMP>#g; s/run-[0-9]+-[0-9]+/<RUN>/g; s/[0-9]{10,}/<TS>/g"; }

# behavioral parity: stdout+stderr+exit-code must match.
_check() {
  local name="$1"; shift
  local b p brc prc
  b="$(MINI_ORK_RUNTIME=bash   "$@" 2>&1 | _norm)"; brc=$?
  p="$(MINI_ORK_RUNTIME=python "$@" 2>&1 | _norm)"; prc=$?
  if [ "$b" = "$p" ] && [ "$brc" = "$prc" ]; then
    printf '  [ok]   %s\n' "$name"
  else
    printf '  [FAIL] %s (rc bash=%s py=%s)\n' "$name" "$brc" "$prc"; FAILS=$((FAILS+1))
    diff <(printf '%s\n' "$b") <(printf '%s\n' "$p") | sed 's/^/       /' | head -12
  fi
}

# exit-code-only parity: for --help/usage, where the ported one-line stubs are a
# documented COSMETIC gap (help text ≠ runtime behavior); both must still exit 0.
_check_rc() {
  local name="$1" want="$2"; shift 2
  local brc prc
  MINI_ORK_RUNTIME=bash   "$@" >/dev/null 2>&1; brc=$?
  MINI_ORK_RUNTIME=python "$@" >/dev/null 2>&1; prc=$?
  if [ "$brc" = "$prc" ] && { [ -z "$want" ] || [ "$brc" = "$want" ]; }; then
    printf '  [ok]   %s (rc=%s)\n' "$name" "$brc"
  else
    printf '  [FAIL] %s (rc bash=%s py=%s want=%s)\n' "$name" "$brc" "$prc" "${want:-any}"; FAILS=$((FAILS+1))
  fi
}

echo "── runtime parity harness (bash vs python) ──"
echo "  behavioral (strict stdout+stderr+rc):"
_check "version"        "$BIN/mini-ork" version
_check "help"           "$BIN/mini-ork" help
_check "doctor"         "$BIN/mini-ork" doctor
_check "unknown-subcmd" "$BIN/mini-ork" bogus-subcmd

echo "  --help/usage (exit-code parity; text is a cosmetic follow-up):"
_check_rc "plan --help"      0 "$BIN/mini-ork-plan" --help
_check_rc "classify --help"  0 "$BIN/mini-ork-classify" --help
_check_rc "conductor --help" 0 "$BIN/mini-ork-conductor" --help
_check_rc "scheduler --help" 0 "$BIN/mini-ork-scheduler" --help
_check_rc "epics --help"     0 "$BIN/mini-ork-epics" --help
_check_rc "execute --help"   0 "$BIN/mini-ork-execute" --help

# plan --dry-run: a full deterministic pipeline (classify→profile→plan placeholder)
printf '# Ship widget\n\n## Success\n- widget renders\n\n## Verification commands\n- `pytest`\n' > "$TMP/k.md"
_dryplan() {   # runtime passed as $1; writes plan.json, prints its content
  local rt="$1" home="$TMP/home-$1"
  MINI_ORK_RUNTIME="$rt" MINI_ORK_HOME="$home" MINI_ORK_TASK_CLASS=code_fix \
    "$BIN/mini-ork-plan" "$TMP/k.md" --out "$TMP/plan-$rt.json" --dry-run >/dev/null 2>&1
  cat "$TMP/plan-$rt.json" 2>/dev/null | _norm
}
if [ "$(_dryplan bash)" = "$(_dryplan python)" ]; then
  echo "  [ok]   plan --dry-run (full pipeline)"
else
  echo "  [FAIL] plan --dry-run (full pipeline)"; FAILS=$((FAILS+1))
  diff <(_dryplan bash) <(_dryplan python) | sed 's/^/       /' | head -12
fi

echo "──"
if [ "$FAILS" -eq 0 ]; then
  echo "PASS — python runtime matches bash across all deterministic checks."
  exit 0
fi
echo "FAIL — $FAILS check(s) diverged; do NOT flip the default runtime yet."
exit 1
