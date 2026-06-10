#!/usr/bin/env bash
# Verifier for obs-smoke: checks the researcher wrote lens-tiny.md and the
# reviewer's JSON verdict exists. Deterministic — no LLM cost.
#
# Emits JSON to stdout (consumed by bin/mini-ork-execute) + writes the
# canonical verifier-result-lens-exists.json sidecar to the run dir.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:-.}"
LENS="$RUN_DIR/lens-tiny.md"
REVIEW="$RUN_DIR/review-tiny_reviewer.json"

pass=true
reasons=()

if [ ! -f "$LENS" ]; then
  pass=false
  reasons+=("lens-tiny.md missing at $LENS")
elif [ "$(wc -c < "$LENS" | tr -d ' ')" -lt 30 ]; then
  pass=false
  reasons+=("lens-tiny.md too small (<30 bytes — researcher likely no-op'd)")
fi

if [ ! -f "$REVIEW" ]; then
  pass=false
  reasons+=("review-tiny_reviewer.json missing at $REVIEW")
fi

# Emit JSON to stdout (consumed by the executor) + write sidecar.
# Pass bash state via env vars to keep the python clean.
export MO_PASS="$pass" MO_LENS="$LENS" MO_REVIEW="$REVIEW"
MO_REASONS=$(printf '%s\n' "${reasons[@]}")
export MO_REASONS

result=$(python3 - <<'PY'
import json, os
reasons = [l.strip() for l in os.environ.get('MO_REASONS','').split('\n') if l.strip()]
print(json.dumps({
    'verifier': 'lens-exists',
    'pass': os.environ.get('MO_PASS') == 'true',
    'reasons': reasons,
    'lens_path': os.environ.get('MO_LENS', ''),
    'review_path': os.environ.get('MO_REVIEW', ''),
}))
PY
)
echo "$result"
# Persist the sidecar for the obs UI's Why? panel to consume
echo "$result" > "${RUN_DIR}/verifier-result-lens-exists.json" 2>/dev/null || true
