#!/usr/bin/env bash
# Fast deterministic gate. Runs in under 10s when project tooling supports it.
# Exits 0 with JSON to stdout per mini-ork verifier contract.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?}"
TOUCHED="$(jq -r '.touched_files[]' "$RUN_DIR/implementer-summary.json" | tr '\n' ' ')"

EVIDENCE="$RUN_DIR/tier1-evidence.log"
exec 3>"$EVIDENCE"

if [ -f "package.json" ] && grep -q 'type-check:touched' package.json; then
  pnpm type-check:touched $TOUCHED 2>&1 | tee /dev/fd/3
  tsc_rc=${PIPESTATUS[0]}
else
  tsc_rc=0
fi

if [ -f ".eslintrc" ] || [ -f ".eslintrc.json" ] || [ -f "eslint.config.js" ]; then
  npx eslint $TOUCHED 2>&1 | tee /dev/fd/3 || lint_rc=$?
  lint_rc=${lint_rc:-0}
else
  lint_rc=0
fi

pass=true
[ "$tsc_rc" -ne 0 ] && pass=false
[ "$lint_rc" -ne 0 ] && pass=false

PASS="$pass" EVIDENCE_PATH="$EVIDENCE" TSC_RC="$tsc_rc" LINT_RC="$lint_rc" python3 -c "import json, os; print(json.dumps({
  'verifier': 'tier1-compile-typecheck',
  'pass': os.environ['PASS'] == 'true',
  'evidence_path': os.environ['EVIDENCE_PATH'],
  'tsc_rc': int(os.environ['TSC_RC']),
  'lint_rc': int(os.environ['LINT_RC']),
}))"
exit 0
