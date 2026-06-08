#!/usr/bin/env bash
# verifiers/bottlenecks-found.sh — gate that the bottleneck scanner
# produced an actionable ranked list and the opus synthesizer ranked
# at least one patch.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by mini-ork-execute)
#
# Output: JSON to stdout. Exit 0 always (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-bottlenecks-found.log"
exec 3>"$EVIDENCE"

missing=()

SCAN="$RUN_DIR/bottleneck-scan.md"
SYNTH="$RUN_DIR/synthesis.md"
ARXIV="$RUN_DIR/arxiv-refs.md"
[ -f "$RUN_DIR/arxiv-research.md" ] && ARXIV="$RUN_DIR/arxiv-research.md"

[ -f "$SCAN" ]  || missing+=("bottleneck-scan.md")
[ -f "$SYNTH" ] || missing+=("synthesis.md")
[ -f "$ARXIV" ] || missing+=("arxiv-refs.md (or arxiv-research.md)")

# Converged → pass with a soft signal so the outer runner terminates.
converged=0
if [ -f "$SCAN" ] && grep -qi "^## Status: converged" "$SCAN"; then
  converged=1
  echo "scanner reported convergence" >&3
fi

ranked_rows=0
if [ -f "$SYNTH" ]; then
  # Count rows in the ranked patch table (lines starting with `| 1 ` … `| 5 `)
  ranked_rows=$(grep -cE '^\| *[1-5] +\|' "$SYNTH" 2>/dev/null || echo 0)
  echo "synthesis ranked_rows=$ranked_rows" >&3

  # Reject polluted synthesis — leaked CLI / learning-mode envelopes
  if grep -qE '^★ Insight ─|<z-insight>' "$SYNTH"; then
    missing+=("synthesis.md contains leaked CLI envelope (★ Insight or <z-insight>)")
  fi
fi

# Pass condition: either converged, or we have all 3 artifacts AND >=1 ranked patch
pass=0
if [ "$converged" -eq 1 ]; then
  pass=1
elif [ "${#missing[@]}" -eq 0 ] && [ "$ranked_rows" -ge 1 ]; then
  pass=1
fi

python3 - "$pass" "$ranked_rows" "$converged" "$EVIDENCE" "${missing[@]}" <<'PY'
import json, sys
pass_, ranked, converged, ev, *missing = sys.argv[1:]
print(json.dumps({
    "verifier": "bottlenecks-found",
    "pass": pass_ == "1",
    "evidence_path": ev,
    "ranked_patches": int(ranked),
    "converged": converged == "1",
    "missing": missing,
}))
PY
