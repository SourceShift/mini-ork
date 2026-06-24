#!/usr/bin/env bash
# Runs tests under adjacent __tests__ directories for touched files. Avoids
# whole-repo test runs that exceed iteration budget.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?}"
TOUCHED="$(jq -r '.touched_files[]' "$RUN_DIR/implementer-summary.json")"
READY="$(jq -r '.ready_for_tier1 // false' "$RUN_DIR/implementer-summary.json" 2>/dev/null || echo false)"

EVIDENCE="$RUN_DIR/tier2-evidence.log"
exec 3>"$EVIDENCE"

if [ "$READY" != "true" ] || [ -z "$(printf '%s' "$TOUCHED" | tr -d '[:space:]')" ]; then
  echo "implementer-summary is not ready for scoped unit tests or touched_files is empty" >&3
  PASS=false EVIDENCE_PATH="$EVIDENCE" JEST_RC=1 TEST_GLOBS_TEXT="" python3 -c "import json, os; print(json.dumps({
    'verifier': 'tier2-scoped-unit',
    'pass': False,
    'evidence_path': os.environ['EVIDENCE_PATH'],
    'reason': 'no implementer artifact ready_for_tier1=true with non-empty touched_files',
    'jest_rc': int(os.environ['JEST_RC']),
    'test_globs': os.environ['TEST_GLOBS_TEXT'],
  }))"
  exit 1
fi

TEST_FILES=()
add_test_file() {
  local candidate="$1"
  [ -f "$candidate" ] || return 0
  case "$candidate" in
    *.test.ts|*.test.tsx|*.spec.ts|*.spec.tsx) TEST_FILES+=("$candidate") ;;
  esac
}

for f in $TOUCHED; do
  dir=$(dirname "$f")
  base=$(basename "$f")
  stem="${base%.*}"
  add_test_file "$f"
  if [ -d "$dir/__tests__" ]; then
    for candidate in \
      "$dir/__tests__/$stem.test.ts" \
      "$dir/__tests__/$stem.test.tsx" \
      "$dir/__tests__/$stem.spec.ts" \
      "$dir/__tests__/$stem.spec.tsx"; do
      add_test_file "$candidate"
    done
  fi
done

if [ "${#TEST_FILES[@]}" -gt 0 ]; then
  mapfile -t TEST_FILES < <(printf '%s\n' "${TEST_FILES[@]}" | awk '!seen[$0]++')
fi

if [ "${#TEST_FILES[@]}" -eq 0 ]; then
  if printf '%s\n' "$TOUCHED" | grep -Eq '\.(ts|tsx)$'; then
    echo "no scoped tests found for touched TypeScript files - failing closed" >&3
    pass=false
    jest_rc=1
  else
    echo "no scoped Jest tests for non-TypeScript touched files - passing with verifier/local evidence only" >&3
    pass=true
    jest_rc=0
  fi
else
  if ls vitest.config.* config/vitest.config.* >/dev/null 2>&1 || grep -q '"vitest"' package.json 2>/dev/null; then
    # vitest project (e.g. Orca): jest+babel can't parse its TS test files
    # ("0 tests, 1 suite failed"). Use vitest with the repo's config.
    _vcfg=""
    for c in config/vitest.config.ts vitest.config.ts vitest.config.mts vitest.config.js; do
      [ -f "$c" ] && _vcfg="$c" && break
    done
    if [ -n "$_vcfg" ]; then
      npx vitest run --config "$_vcfg" "${TEST_FILES[@]}" 2>&1 | tee /dev/fd/3
    else
      npx vitest run "${TEST_FILES[@]}" 2>&1 | tee /dev/fd/3
    fi
  elif [ -f "server/jest.config.js" ]; then
    JEST_GUARD_SOFT=1 pnpm test:server --runTestsByPath "${TEST_FILES[@]}" 2>&1 | tee /dev/fd/3
  else
    npx jest --runTestsByPath "${TEST_FILES[@]}" 2>&1 | tee /dev/fd/3
  fi
  jest_rc=${PIPESTATUS[0]}
  [ "$jest_rc" -eq 0 ] && pass=true || pass=false
fi

PASS="$pass" EVIDENCE_PATH="$EVIDENCE" JEST_RC="$jest_rc" TEST_GLOBS_TEXT="${TEST_FILES[*]}" python3 -c "import json, os; print(json.dumps({
  'verifier': 'tier2-scoped-unit',
  'pass': os.environ['PASS'] == 'true',
  'evidence_path': os.environ['EVIDENCE_PATH'],
  'jest_rc': int(os.environ['JEST_RC']),
  'test_files': os.environ['TEST_GLOBS_TEXT'],
}))"
[ "$pass" = "true" ] && exit 0
exit 1
