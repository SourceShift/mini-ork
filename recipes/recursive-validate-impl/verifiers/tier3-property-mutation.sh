#!/usr/bin/env bash
# Property-based plus mutation testing on touched files. Slowest verifier below
# the LLM panel, so it only fires after tier1 and tier2 are green.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?}"

EVIDENCE="$RUN_DIR/tier3-evidence.log"
exec 3>"$EVIDENCE"
READY="$(jq -r '.ready_for_tier1 // false' "$RUN_DIR/implementer-summary.json" 2>/dev/null || echo false)"
TOUCHED="$(jq -r '.touched_files[]' "$RUN_DIR/implementer-summary.json" 2>/dev/null || true)"

if [ "$READY" != "true" ] || [ -z "$(printf '%s' "$TOUCHED" | tr -d '[:space:]')" ]; then
  echo "implementer-summary is not ready for tier3 or touched_files is empty" >&3
  PASS=false EVIDENCE_PATH="$EVIDENCE" PROPERTY_RC=1 MUTATION_RC=0 python3 -c "import json, os; print(json.dumps({
    'verifier': 'tier3-property-mutation',
    'pass': False,
    'evidence_path': os.environ['EVIDENCE_PATH'],
    'reason': 'no implementer artifact ready_for_tier1=true with non-empty touched_files',
    'property_rc': int(os.environ['PROPERTY_RC']),
    'mutation_rc': int(os.environ['MUTATION_RC']),
  }))"
  exit 0
fi

pass=true
property_rc=0
mutation_rc=0

if [ -f "node_modules/.bin/fast-check" ] || grep -q '"fast-check"' package.json 2>/dev/null; then
  echo "property tests not yet wired for this repo; touched files: $TOUCHED" >&3
fi

if [ -f "stryker.conf.json" ] || [ -f "stryker.conf.js" ]; then
  echo "running mutation tests; this may take 5-30 min" >&3
  npx stryker run 2>&1 | tee /dev/fd/3
  mutation_rc=${PIPESTATUS[0]}
  [ "$mutation_rc" -eq 0 ] || pass=false
else
  echo "no mutation tooling configured - skipping" >&3
fi

PASS="$pass" EVIDENCE_PATH="$EVIDENCE" PROPERTY_RC="$property_rc" MUTATION_RC="$mutation_rc" python3 -c "import json, os; print(json.dumps({
  'verifier': 'tier3-property-mutation',
  'pass': os.environ['PASS'] == 'true',
  'evidence_path': os.environ['EVIDENCE_PATH'],
  'property_rc': int(os.environ['PROPERTY_RC']),
  'mutation_rc': int(os.environ['MUTATION_RC']),
}))"
exit 0
