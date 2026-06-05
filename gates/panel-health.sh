#!/usr/bin/env bash
# gates/panel-health.sh — recipe-level CW-POR panel-health gate shim.
#
# Wraps lib/cw_por.sh::mo_compute_cw_por for invocation via gate_registry.
# Detects authority-capture (one confidently-stated voice dragging the
# panel toward a wrong answer) — orthogonal to Krippendorff α.
#
# Context JSON contract:
#   { "verdict_file": "<path-to-panel-verdict.json>" }
#
# Env knob (forwarded to the lib):
#   MO_CW_POR_THRESHOLD  default 0.3
#
# rc:
#   0 → pass (panel_healthy OR indeterminate-no-ground-truth)
#   1 → fail (authority_capture_suspected)
#   2 → defer (malformed input / lib unloadable)

set +e
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

context="${1:-}"
if [ -z "$context" ]; then
  printf '{"verdict":"defer","reason":"no context json"}\n'
  exit 2
fi

verdict_file=$(printf '%s' "$context" | jq -r '.verdict_file // empty' 2>/dev/null)
if [ -z "$verdict_file" ] || [ ! -f "$verdict_file" ]; then
  printf '{"verdict":"defer","reason":"verdict_file missing or not found"}\n'
  exit 2
fi

# shellcheck source=../lib/cw_por.sh
source "$MINI_ORK_ROOT/lib/cw_por.sh" 2>/dev/null || {
  printf '{"verdict":"defer","reason":"cw_por lib not loadable"}\n'
  exit 2
}

result=$(mo_compute_cw_por "$verdict_file" 2>/dev/null)
verdict=$(printf '%s' "$result" | jq -r '.verdict // "indeterminate"' 2>/dev/null)
printf '%s\n' "$result"

case "$verdict" in
  panel_healthy|indeterminate)   exit 0 ;;
  authority_capture_suspected)   exit 1 ;;
  *)                             exit 2 ;;
esac
