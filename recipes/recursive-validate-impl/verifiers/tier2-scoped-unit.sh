#!/usr/bin/env bash
# Runs tests under adjacent __tests__ directories for touched files. Avoids
# whole-repo test runs that exceed iteration budget.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?}"
TOUCHED="$(jq -r '.touched_files[]' "$RUN_DIR/implementer-summary.json")"

TEST_GLOBS=()
for f in $TOUCHED; do
  dir=$(dirname "$f")
  if [ -d "$dir/__tests__" ]; then
    TEST_GLOBS+=("$dir/__tests__")
  fi
  parent=$(dirname "$dir")
  if [ -d "$parent/__tests__" ]; then
    TEST_GLOBS+=("$parent/__tests__")
  fi
done

EVIDENCE="$RUN_DIR/tier2-evidence.log"
exec 3>"$EVIDENCE"

if [ "${#TEST_GLOBS[@]}" -eq 0 ]; then
  echo "no scoped tests found - pass vacuously" >&3
  pass=true
  jest_rc=0
else
  npx jest --testPathPattern="${TEST_GLOBS[*]}" 2>&1 | tee /dev/fd/3
  jest_rc=${PIPESTATUS[0]}
  [ "$jest_rc" -eq 0 ] && pass=true || pass=false
fi

PASS="$pass" EVIDENCE_PATH="$EVIDENCE" JEST_RC="$jest_rc" TEST_GLOBS_TEXT="${TEST_GLOBS[*]}" python3 -c "import json, os; print(json.dumps({
  'verifier': 'tier2-scoped-unit',
  'pass': os.environ['PASS'] == 'true',
  'evidence_path': os.environ['EVIDENCE_PATH'],
  'jest_rc': int(os.environ['JEST_RC']),
  'test_globs': os.environ['TEST_GLOBS_TEXT'],
}))"
exit 0
