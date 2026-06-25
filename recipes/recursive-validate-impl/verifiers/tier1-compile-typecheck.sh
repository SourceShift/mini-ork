#!/usr/bin/env bash
# Fast deterministic gate. Runs in under 10s when project tooling supports it.
# Exits 0 with JSON to stdout per mini-ork verifier contract.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?}"
mapfile -t TOUCHED_FILES < <(jq -r '.touched_files[]?' "$RUN_DIR/implementer-summary.json")
READY="$(jq -r '.ready_for_tier1 // false' "$RUN_DIR/implementer-summary.json" 2>/dev/null || echo false)"

EVIDENCE="$RUN_DIR/tier1-evidence.log"
exec 3>"$EVIDENCE"

if [ "$READY" != "true" ] || [ "${#TOUCHED_FILES[@]}" -eq 0 ]; then
  echo "implementer-summary is not ready for tier1 or touched_files is empty" >&3
  PASS=false EVIDENCE_PATH="$EVIDENCE" TSC_RC=1 LINT_RC=0 python3 -c "import json, os; print(json.dumps({
    'verifier': 'tier1-compile-typecheck',
    'pass': False,
    'evidence_path': os.environ['EVIDENCE_PATH'],
    'reason': 'no implementer artifact ready_for_tier1=true with non-empty touched_files',
    'tsc_rc': int(os.environ['TSC_RC']),
    'lint_rc': int(os.environ['LINT_RC']),
  }))"
  exit 1
fi

SOURCE_FILES=()
SHELL_FILES=()
ARTIFACT_FILES=()
for path in "${TOUCHED_FILES[@]}"; do
  case "$path" in
    .mini-ork/runs/*|*/.mini-ork/runs/*)
      ARTIFACT_FILES+=("$path")
      ;;
    *.ts|*.tsx|*.mts|*.cts|*.js|*.jsx|*.mjs|*.cjs)
      SOURCE_FILES+=("$path")
      ;;
    *.sh)
      SHELL_FILES+=("$path")
      ;;
  esac
done

{
  printf 'touched_files=%s\n' "${#TOUCHED_FILES[@]}"
  printf 'source_files=%s\n' "${#SOURCE_FILES[@]}"
  printf 'shell_files=%s\n' "${#SHELL_FILES[@]}"
  printf 'artifact_files=%s\n' "${#ARTIFACT_FILES[@]}"
} >&3

tsc_rc=0
lint_rc=0
shell_rc=0

if [ "${#SOURCE_FILES[@]}" -gt 0 ] && [ -f "package.json" ] && grep -q 'type-check:touched' package.json; then
  pnpm type-check:touched "${SOURCE_FILES[@]}" 2>&1 | tee /dev/fd/3
  tsc_rc=${PIPESTATUS[0]}
else
  echo "no TypeScript/JavaScript source files for tier1 typecheck" >&3
fi

if [ "${#SOURCE_FILES[@]}" -gt 0 ] && { [ -f ".eslintrc" ] || [ -f ".eslintrc.json" ] || [ -f "eslint.config.js" ] || [ -f "eslint.config.mjs" ]; }; then
  npx eslint "${SOURCE_FILES[@]}" 2>&1 | tee /dev/fd/3 || lint_rc=$?
  lint_rc=${lint_rc:-0}
else
  echo "no source files for eslint or no eslint config found" >&3
fi

if [ "${#SHELL_FILES[@]}" -gt 0 ]; then
  for path in "${SHELL_FILES[@]}"; do
    if ! bash -n "$path" 2>&1 | tee /dev/fd/3; then
      shell_rc=1
    fi
  done
fi

pass=true
[ "$tsc_rc" -ne 0 ] && pass=false
[ "$lint_rc" -ne 0 ] && pass=false
[ "$shell_rc" -ne 0 ] && pass=false

PASS="$pass" EVIDENCE_PATH="$EVIDENCE" TSC_RC="$tsc_rc" LINT_RC="$lint_rc" SHELL_RC="$shell_rc" python3 -c "import json, os; print(json.dumps({
  'verifier': 'tier1-compile-typecheck',
  'pass': os.environ['PASS'] == 'true',
  'evidence_path': os.environ['EVIDENCE_PATH'],
  'tsc_rc': int(os.environ['TSC_RC']),
  'lint_rc': int(os.environ['LINT_RC']),
  'shell_rc': int(os.environ['SHELL_RC']),
}))"
[ "$pass" = "true" ] && exit 0
exit 1
