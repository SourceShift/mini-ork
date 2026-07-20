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
# Usage: bash scripts/runtime-parity-harness.sh [fork]
# Exit 0 = every check identical across runtimes; 1 = a divergence.
set -uo pipefail

ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT="$ROOT"
FORK="${1:-}"

# A closed fork no longer has a Bash runtime to invoke. Its pre-retirement
# oracle receipt is durable in the migration run, so the post-retirement
# harness validates both that receipt and the standalone Python contract. Keep
# no-argument mode as the full cross-runtime matrix for forks still in flight.
if [ -n "$FORK" ]; then
  FORK_TEST="$ROOT/tests/unit/test_mini_ork_${FORK}_py.py"
  if [ ! -f "$FORK_TEST" ]; then
    echo "FAIL — no focused runtime contract for fork '$FORK': $FORK_TEST" >&2
    exit 1
  fi
  if [ "$FORK" = "cli" ]; then
    PRE_REPORT="${MO_PRE_RETIREMENT_REPORT:?MO_PRE_RETIREMENT_REPORT required for cli closure}"
    PRE_EVIDENCE="${MO_PRE_RETIREMENT_EVIDENCE:?MO_PRE_RETIREMENT_EVIDENCE required for cli closure}"
    python3 - "$PRE_REPORT" "$PRE_EVIDENCE" <<'PY'
import json
import os
import sys

report, evidence = sys.argv[1:]
if not os.path.isfile(evidence) or os.path.getsize(evidence) == 0:
    raise SystemExit("pre-retirement evidence is missing or empty")
with open(report, encoding="utf-8") as handle:
    state = json.load(handle)
if state.get("fork") != "cli" or state.get("pass") is not True:
    raise SystemExit("pre-retirement CLI oracle did not pass")
print(f"[ok] durable pre-retirement oracle: {report}")
PY
  fi
  echo "── focused post-retirement contract: $FORK ──"
  exec python3 -m pytest "$FORK_TEST" -q -p no:cacheprovider
fi

BIN="$ROOT/bin"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
FAILS=0

# normalize volatile bits (tmp paths, run ids, timestamps) so only real
# behavioral differences surface.
_norm() { sed -E "s#${TMP}[^ ]*#<TMP>#g; s/run-[0-9]+-[0-9]+/<RUN>/g; s/[0-9]{10,}/<TS>/g"; }

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

echo "── runtime parity harness (bash vs python) ──"
echo "  behavioral (strict stdout+stderr+rc):"
_check "version"        "$BIN/mini-ork" version
_check "help"           "$BIN/mini-ork" help
_check "doctor"         "$BIN/mini-ork" doctor
_check "unknown-subcmd" "$BIN/mini-ork" bogus-subcmd
_check "review no-arg"  "$BIN/mini-ork-review"
_check "review bad-sub" "$BIN/mini-ork-review" bogus

echo "  --help/usage (strict stdout+stderr+rc — full usage text transcribed):"
_check "plan --help"      env PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.ported.mini_ork_plan --help
_check "classify --help"  env PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m mini_ork.ported.mini_ork_classify --help
_check "conductor --help" "$BIN/mini-ork-conductor" --help
_check "scheduler --help" "$BIN/mini-ork-scheduler" --help
_check "epics --help"     "$BIN/mini-ork-epics" --help
_check "execute --help"   "$BIN/mini-ork-execute" --help

# plan --dry-run: a full deterministic pipeline (classify→profile→plan placeholder)
printf '# Ship widget\n\n## Success\n- widget renders\n\n## Verification commands\n- `pytest`\n' > "$TMP/k.md"
_dryplan() {   # runtime passed as $1; writes plan.json, prints its content
  local rt="$1" home="$TMP/home-$1"
  MINI_ORK_RUNTIME="$rt" MINI_ORK_HOME="$home" MINI_ORK_TASK_CLASS=code_fix \
    env PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m mini_ork.ported.mini_ork_plan "$TMP/k.md" \
      --out "$TMP/plan-$rt.json" --dry-run >/dev/null 2>&1
  cat "$TMP/plan-$rt.json" 2>/dev/null | _norm
}
if [ "$(_dryplan bash)" = "$(_dryplan python)" ]; then
  echo "  [ok]   plan --dry-run (full pipeline)"
else
  echo "  [FAIL] plan --dry-run (full pipeline)"; FAILS=$((FAILS+1))
  diff <(_dryplan bash) <(_dryplan python) | sed 's/^/       /' | head -12
fi

# init: scaffolds .mini-ork under a fresh project dir. Run each runtime in its
# own temp project (side-effecting) and diff stdout with the project path
# normalized. Guards the silent-no-op class of cutover bug (a delegated module
# with no working __main__ that does nothing under python).
_initrun() {  # runtime passed as $1; prints init stdout with project path normalized
  local rt="$1" proj="$TMP/proj-$1"
  mkdir -p "$proj"
  ( cd "$proj" && MINI_ORK_RUNTIME="$rt" MINI_ORK_ROOT="$ROOT" "$BIN/mini-ork-init" 2>&1 ) \
    | sed "s#$proj#<PROJ>#g" | _norm
}
if [ "$(_initrun bash)" = "$(_initrun python)" ]; then
  echo "  [ok]   init (scaffold .mini-ork)"
else
  echo "  [FAIL] init (scaffold .mini-ork)"; FAILS=$((FAILS+1))
  diff <(_initrun bash) <(_initrun python) | sed 's/^/       /' | head -12
fi

echo "──"
if [ "$FAILS" -eq 0 ]; then
  echo "PASS — python runtime matches bash across all deterministic checks."
  exit 0
fi
echo "FAIL — $FAILS check(s) diverged; do NOT flip the default runtime yet."
exit 1
