#!/usr/bin/env bash
# cs_dispatch.sh — context-strategy dispatcher. The single entry point
# for the native executor (and downstream callers) to prepare per-lens
# context via the strategy registry.
#
# Public API:
#   cs_dispatch <strategy_name> <input_path> <output_path> <lens_name>
#
# Strategy registry — names → prepare-fn:
#   passthrough      → cs_passthrough_prepare
#   chunk_fixed      → cs_chunk_fixed_prepare
#   chunk_semantic   → cs_chunk_semantic_prepare
#   reorder_shuffle  → cs_reorder_shuffle_prepare
#
# Adding a new strategy: drop cs_<name>.sh in this dir, register the
# mapping in _CS_REGISTRY below, smoke test, done.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

CS_DIR="${CS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# Strategy registry — name → source-file (relative to CS_DIR)
declare -A _CS_REGISTRY=(
  [passthrough]=cs_passthrough.sh
  [chunk_fixed]=cs_chunk_fixed.sh
  [chunk_semantic]=cs_chunk_semantic.sh
  [reorder_shuffle]=cs_reorder_shuffle.sh
)

cs_list_strategies() {
  for name in "${!_CS_REGISTRY[@]}"; do
    echo "$name"
  done | sort
}

cs_dispatch() {
  local strategy="${1:?strategy_name required}"
  local input="${2:?input_path required}"
  local output="${3:?output_path required}"
  local lens="${4:-default}"

  if [ -z "${_CS_REGISTRY[$strategy]:-}" ]; then
    echo "cs_dispatch: unknown strategy '$strategy'. Available:" >&2
    cs_list_strategies >&2
    return 2
  fi

  local strategy_file="$CS_DIR/${_CS_REGISTRY[$strategy]}"
  if [ ! -f "$strategy_file" ]; then
    echo "cs_dispatch: strategy file missing: $strategy_file" >&2
    return 3
  fi

  # shellcheck source=/dev/null
  source "$strategy_file"

  local fn="cs_${strategy}_prepare"
  if ! declare -f "$fn" > /dev/null 2>&1; then
    echo "cs_dispatch: prepare function $fn not defined after sourcing $strategy_file" >&2
    return 4
  fi

  "$fn" "$input" "$output" "$lens"
}

# When invoked directly: print available strategies + smoke each on a
# fixture input.
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "=== cs_dispatch — available strategies ==="
  cs_list_strategies
  echo
  if [ "${1:-}" = "--smoke" ]; then
    fixture=$(mktemp -t cs_fixture.XXXXXX.md)
    cat > "$fixture" <<'FIX'
# Section A

This is paragraph one. It contains content for testing.

# Section B

Paragraph two; another distinct section. With more text to fill the
chunk and exercise the splitting logic.

# Section C

Final section. Three paragraphs total. Each cs strategy should yield
a different output shape when called per-lens.
FIX
    for strategy in $(cs_list_strategies); do
      out=$(mktemp -t cs_out_${strategy}.XXXXXX.md)
      echo "--- $strategy → $out ---"
      cs_dispatch "$strategy" "$fixture" "$out" "smoke_lens"
      head -3 "$out"
      echo
    done
    rm -f "$fixture"
  fi
fi
