#!/usr/bin/env bash
# Project-aware verifier for epic-runner child runs targeting the researcher repo.
#
# This script is intended to be passed through MINI_ORK_EPIC_VERIFIER_SCRIPT.
# It runs only when epic-runner invokes it against a researcher epic, not during
# framework-edit's own static verifier dispatch.

set -uo pipefail

NAME="researcher"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_lib.sh"

_evidence_log_init "$NAME"

REPO="${MINI_ORK_EPIC_TARGET_REPO:-$HOME/ps/researcher}"
CHANGED_FILE_LIST="$RUN_DIR/verifier-$NAME.changed-files"
TYPECHECK_FILE_LIST="$RUN_DIR/verifier-$NAME.typecheck-files"
JEST_INPUT_LIST="$RUN_DIR/verifier-$NAME.jest-inputs"

_normalize_changed_files() {
  : >"$CHANGED_FILE_LIST"
  if [ -n "${EPIC_CHANGED_FILES:-}" ]; then
    printf '%s\n' "$EPIC_CHANGED_FILES" |
      sed 's#^\./##' |
      sed '/^[[:space:]]*$/d' |
      sort -u >"$CHANGED_FILE_LIST"
  fi
}

_collect_typecheck_files() {
  : >"$TYPECHECK_FILE_LIST"
  while IFS= read -r path; do
    case "$path" in
      *.ts|*.tsx)
        printf '%s\n' "$path" >>"$TYPECHECK_FILE_LIST"
        ;;
    esac
  done <"$CHANGED_FILE_LIST"
  sort -u "$TYPECHECK_FILE_LIST" -o "$TYPECHECK_FILE_LIST"
}

_typecheck_touched() {
  if [ ! -s "$TYPECHECK_FILE_LIST" ]; then
    echo "skip: no .ts/.tsx files changed" >&3
    return 0
  fi
  if ! _check_pnpm_workspace "$REPO"; then
    echo "pnpm workspace missing at $REPO; cannot run type-check:touched" >&3
    return 1
  fi
  mapfile -t typecheck_files <"$TYPECHECK_FILE_LIST"
  pnpm --dir "$REPO" type-check:touched "${typecheck_files[@]}"
}

_test_candidates_for_changed_file() {
  local path="$1" dir base stem ext
  dir="$(dirname "$path")"
  base="$(basename "$path")"
  case "$base" in
    *.test.ts|*.test.tsx|*.spec.ts|*.spec.tsx)
      [ -f "$REPO/$path" ] && printf '%s\n' "$path"
      return 0
      ;;
  esac

  stem="${base%.*}"
  ext="${base##*.}"
  if [ "$ext" = "ts" ] || [ "$ext" = "tsx" ]; then
    for candidate in \
      "$dir/$stem.test.$ext" \
      "$dir/$stem.spec.$ext" \
      "$dir/__tests__/$stem.test.$ext" \
      "$dir/__tests__/$stem.spec.$ext"; do
      [ -f "$REPO/$candidate" ] && printf '%s\n' "$candidate"
    done
  fi
}

_collect_jest_inputs() {
  : >"$JEST_INPUT_LIST"
  while IFS= read -r path; do
    _test_candidates_for_changed_file "$path"
  done <"$CHANGED_FILE_LIST" | sort -u >"$JEST_INPUT_LIST"
}

_jest_related_tests() {
  if [ ! -s "$JEST_INPUT_LIST" ]; then
    echo "skip: no changed or adjacent jest test files found" >&3
    return 0
  fi
  if [ ! -f "$REPO/server/jest.config.js" ]; then
    echo "missing jest config at $REPO/server/jest.config.js" >&3
    return 1
  fi
  mapfile -t jest_inputs <"$JEST_INPUT_LIST"
  npx --prefix "$REPO" jest \
    --config "$REPO/server/jest.config.js" \
    --findRelatedTests "${jest_inputs[@]}" \
    --runInBand \
    --forceExit
}

_sql_probe() {
  if [ -z "${EPIC_SQL_PROBE:-}" ]; then
    echo "skip: EPIC_SQL_PROBE not set" >&3
    return 0
  fi
  if ! _check_psql_credentials_set; then
    echo "missing one or more PostgreSQL env vars: PGPASSWORD, PGHOST, PGPORT, PGUSER, PGDATABASE" >&3
    return 1
  fi
  PGPASSWORD="$PGPASSWORD" psql \
    -h "$PGHOST" \
    -p "$PGPORT" \
    -U "$PGUSER" \
    -d "$PGDATABASE" \
    -c "$EPIC_SQL_PROBE"
}

_no_uncommitted_debt_files() {
  local debt
  debt="$(git -C "$REPO" status --porcelain -- \
    '*.orig' \
    '*.rej' \
    '*~' \
    '.pytest_cache' \
    'coverage' \
    'server/coverage' 2>/dev/null || true)"
  if [ -n "$debt" ]; then
    printf '%s\n' "$debt" >&3
    return 1
  fi
  return 0
}

_normalize_changed_files
_collect_typecheck_files
_collect_jest_inputs

echo "target_repo=$REPO" >&3
echo "changed_files=$CHANGED_FILE_LIST" >&3
echo "typecheck_files=$TYPECHECK_FILE_LIST" >&3
echo "jest_inputs=$JEST_INPUT_LIST" >&3

_record_check "typecheck-passed" "pnpm type-check:touched passes for changed TypeScript files, or is skipped when none changed" \
  '_typecheck_touched'
_record_check "jest-passed" "jest related tests pass when changed or adjacent tests exist, or are skipped when none apply" \
  '_jest_related_tests'
_record_check "sql-probe-passed" "optional EPIC_SQL_PROBE passes with operator-supplied PostgreSQL credentials, or is skipped when unset" \
  '_sql_probe'
_record_check "no-uncommitted-debt-files" "target repo has no leftover debt artifacts such as .orig, .rej, backups, or coverage directories" \
  '_no_uncommitted_debt_files'

_emit_verifier_json "$NAME" "$REPO $CHANGED_FILE_LIST"

exit 0
