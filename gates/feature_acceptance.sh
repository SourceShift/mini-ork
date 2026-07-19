#!/usr/bin/env bash
# gates/feature_acceptance.sh — end-to-end acceptance probes for mini-ork's core
# promised features. The self-migrate recipe's real finish line: a fork is only
# "migrated correctly" when the FEATURE it sits under still works end-to-end,
# not just when unit-parity passes.
#
# Usage:  bash gates/feature_acceptance.sh <fork-or-feature>
#         bash gates/feature_acceptance.sh all
# Exit 0 = probe passed; non-zero = failed. Prints a one-line PASS/FAIL per probe.
#
# Feature ↔ entrypoint map (from docs/migration/self-migrate-feature-manifest.md):
#   classify plan execute verify reflect  → the 6-stage loop stages
#   routing        → learning_governed router flips (learning-loop-live-validate)
#   learning-loop  → reward→routing closes (learning-loop-closure-gate)
#   verify-gate    → a real cheat is rejected / a real fix passes
#   framework-edit → propose-not-commit self-modification
#   resume         → durable checkpoint resume
#   epics          → dependency-ordered multi-epic delivery

set -uo pipefail
ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TARGET="${1:-all}"
rc_all=0

_run() { # <label> <cmd...>  → prints PASS/FAIL, updates rc_all
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $label"; else echo "FAIL  $label"; rc_all=1; fi
}

# ── entrypoint smoke: the command runs under the Python runtime AND matches bash ──
_probe_entrypoint() { # <cmd>
  local cmd="$1"
  local bin="$ROOT/bin/mini-ork-$cmd"
  local module="mini_ork.ported.mini_ork_$cmd"
  # (a) --help works through the live Python entrypoint. Before retirement this
  # exercises runtime-select delegation; after retirement it exercises the
  # canonical module directly instead of turning deletion into a vacuous SKIP.
  if [ -f "$bin" ]; then
    _run "$cmd:help(python)" env MINI_ORK_RUNTIME=python MINI_ORK_ROOT="$ROOT" bash "$bin" --help
  else
    _run "$cmd:help(python-sole)" env PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m "$module" --help
  fi
  # (b) the ported module imports + exposes main (the runtime path is real)
  _run "$cmd:module-imports" python3 -c "import importlib,sys; sys.path.insert(0,'$ROOT'); m=importlib.import_module('$module'); assert hasattr(m,'main')"
}

# ── higher-order feature probes (delegate to existing gates where they exist) ──
_probe_routing() {
  local s="$ROOT/scripts/learning-loop-live-validate.sh"
  [ -f "$s" ] || { echo "SKIP  routing — no live-validate script"; return 0; }
  _run "routing:inspect" env MINI_ORK_ROOT="$ROOT" bash "$s"   # bare = inspect-only, no LLM
}
_probe_learning_loop() {
  local s="$ROOT/scripts/learning-loop-closure-gate.sh"
  [ -f "$s" ] || { echo "SKIP  learning-loop — no closure gate"; return 0; }
  _run "learning-loop:closure" env MINI_ORK_ROOT="$ROOT" bash "$s"
}

case "$TARGET" in
  classify|plan|execute|verify|reflect) _probe_entrypoint "$TARGET" ;;
  routing)        _probe_routing ;;
  learning-loop)  _probe_learning_loop ;;
  all)
    for c in classify plan execute verify reflect; do _probe_entrypoint "$c"; done
    _probe_routing
    _probe_learning_loop
    ;;
  *) echo "FAIL  unknown feature/fork: $TARGET (see header for the map)"; rc_all=1 ;;
esac

exit $rc_all
