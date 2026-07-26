#!/usr/bin/env bash
# verifiers/lens-completeness.sh — verify all 5 lens reports + anonymous synthesis
# exist, are non-empty, and cite at least one file:line anchor.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "lens-completeness", "pass": bool, "evidence_path": "...",
#     "findings_count": N, "missing": [...] }
#
# Exit codes: always 0 (caller reads .pass from JSON).
set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"

EVIDENCE="$RUN_DIR/verifier-lens-completeness.log"
exec 3>"$EVIDENCE"

missing=()
findings_total=0

for lens in glm kimi codex opus minimax; do
  f="$RUN_DIR/lens-$lens.md"
  if [ ! -f "$f" ]; then
    echo "MISSING: $f" >&3
    missing+=("lens-$lens.md")
    continue
  fi
  # Non-empty + ≥1 file:line anchor (path:digit pattern)
  lines=$(wc -l < "$f" | tr -d ' ')
  anchors=$(grep -cE '[a-zA-Z_./-]+:[0-9]+' "$f" || true)
  echo "lens-$lens: $lines lines, $anchors anchors" >&3
  if [ "$lines" -lt 10 ]; then
    missing+=("lens-$lens.md (too short: $lines lines)")
  fi
  if [ "$anchors" -lt 1 ]; then
    missing+=("lens-$lens.md (no file:line anchors)")
  fi
  findings_total=$((findings_total + lines))
done

# The deterministic transform must materialize exactly one anonymous response
# per lens. It may preserve evidence text but must not expose source filenames
# through the consumer-facing response headings.
panel="$RUN_DIR/panel-responses.md"
if [ ! -f "$panel" ]; then
  missing+=("panel-responses.md")
elif [ "$(grep -cE '^## Response [A-E]$' "$panel" || true)" -ne 5 ]; then
  missing+=("panel-responses.md (expected Response A through Response E)")
fi

# Synthesis must exist + cross-reference all five anonymous responses. The
# synthesizer is deliberately not expected to reveal or recover lane identity.
synth="$RUN_DIR/synthesis.md"
if [ ! -f "$synth" ]; then
  missing+=("synthesis.md")
else
  for response in A B C D E; do
    if ! grep -q "Response $response\|$response-[0-9]" "$synth"; then
      missing+=("synthesis.md (no reference to Response $response)")
    fi
  done
fi

# Compose verdict
if [ "${#missing[@]}" -eq 0 ]; then
  pass=true
  python3 -c "import json; print(json.dumps({
    'verifier': 'lens-completeness',
    'pass': True,
    'evidence_path': '$EVIDENCE',
    'findings_count': $findings_total,
    'missing': []
  }))"
else
  python3 - <<PY
import json
missing = """${missing[@]}""".split()
print(json.dumps({
  'verifier': 'lens-completeness',
  'pass': False,
  'evidence_path': '$EVIDENCE',
  'findings_count': $findings_total,
  'missing': missing
}))
PY
fi

exit 0
