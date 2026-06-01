#!/usr/bin/env bash
# verifiers/source-completeness.sh — verify all 4 lens reports + synthesis
# exist, are non-empty, and cite enough sources.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by mini-ork-execute)
#
# Output: JSON to stdout
#   { "verifier": "source-completeness", "pass": bool, "evidence_path": "...",
#     "source_count": N, "missing": [...] }
#
# Exit codes: always 0 (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"

EVIDENCE="$RUN_DIR/verifier-source-completeness.log"
exec 3>"$EVIDENCE"

missing=()
total_sources=0

for lens in glm kimi codex opus; do
  f="$RUN_DIR/lens-$lens.md"
  if [ ! -f "$f" ]; then
    echo "MISSING: $f" >&3
    missing+=("lens-$lens.md")
    continue
  fi
  # Non-empty + minimum source count check
  lines=$(wc -l < "$f" | tr -d ' ')
  # Source-citation patterns:
  #  - http(s):// URLs
  #  - arxiv:XXXX.XXXXX
  #  - github.com/org/repo
  #  - (Author Year) parenthetical
  sources=$(grep -cE 'https?://|arxiv:|github\.com/|\([A-Z][a-z]+ [0-9]{4}\)' "$f" 2>/dev/null || true)
  echo "lens-$lens: $lines lines, $sources sources" >&3

  if [ "$lines" -lt 20 ]; then
    missing+=("lens-$lens.md (too short: $lines lines)")
  fi
  # Per-lens minimum citation count
  min_required=5
  case "$lens" in
    opus) min_required=3 ;;   # opus is narrative; fewer but deeper citations OK
  esac
  if [ "$sources" -lt "$min_required" ]; then
    missing+=("lens-$lens.md (only $sources sources, need ≥$min_required)")
  fi
  total_sources=$((total_sources + sources))
done

# Synthesis must exist + cross-reference all 4 lens names + use consensus markers
synth="$RUN_DIR/synthesis.md"
if [ ! -f "$synth" ]; then
  missing+=("synthesis.md")
else
  for lens in glm kimi codex opus; do
    if ! grep -qE "(lens-)?$lens" "$synth"; then
      missing+=("synthesis.md (no reference to $lens lens)")
    fi
  done
  # Consensus markers (★ unicode) — at least one should appear if there's any consensus.
  # Soft check; absence is a warning not a fail (legitimately disputed topics may have 0 consensus).
  consensus_count=$(grep -c '★' "$synth" 2>/dev/null || true)
  echo "synthesis.md: $consensus_count consensus marker(s)" >&3
fi

# Compose verdict — pass if no hard-fail items missing
if [ "${#missing[@]}" -eq 0 ]; then
  pass=true
  python3 -c "import json; print(json.dumps({
    'verifier': 'source-completeness',
    'pass': True,
    'evidence_path': '$EVIDENCE',
    'source_count': $total_sources,
    'missing': []
  }))"
else
  python3 - <<PY
import json
missing = """${missing[@]}""".split()
print(json.dumps({
  'verifier': 'source-completeness',
  'pass': False,
  'evidence_path': '$EVIDENCE',
  'source_count': $total_sources,
  'missing': missing
}))
PY
fi

exit 0
