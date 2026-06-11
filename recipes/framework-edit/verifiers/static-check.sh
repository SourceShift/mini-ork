#!/usr/bin/env bash
# verifiers/static-check.sh — run static checks over framework-edit.diff.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory set by mini-ork-execute
#   MINI_ORK_ROOT    — optional repo root
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MINI_ORK_ROOT:-$(pwd)}"
NAME="static-check"
DIFF="$RUN_DIR/framework-edit.diff"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"
CHANGED="$RUN_DIR/verifier-$NAME.changed-files"
WORK_PARENT="$RUN_DIR/verifier-$NAME-work"
WORKTREE="$WORK_PARENT/repo"
: >"$CHECKS_TSV"
: >"$CHANGED"
exec 3>"$EVIDENCE"

_check() {
  local id="$1" desc="$2" cond="$3"
  echo "[$id] $desc" >&3
  if eval "$cond" >&3 2>&1; then
    printf '%s\t%s\ttrue\n' "$id" "$desc" >>"$CHECKS_TSV"
    echo "  ok" >&3
  else
    printf '%s\t%s\tfalse\n' "$id" "$desc" >>"$CHECKS_TSV"
    echo "  FAIL" >&3
  fi
}

_changed_files() {
  git -C "$REPO_ROOT" apply --numstat "$DIFF" 2>/dev/null | awk '{print $3}' | sed '/^$/d' | sort -u >"$CHANGED"
}

_make_patched_copy() {
  rm -rf "$WORK_PARENT"
  mkdir -p "$WORKTREE"
  git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORKTREE"
  git -C "$WORKTREE" apply "$DIFF"
}

# Template tier: declared artifacts exist and have basic shape.
_check "artifact-diff-exists" "framework-edit.diff exists" '[ -f "$DIFF" ]'
_check "artifact-diff-non-empty" "framework-edit.diff is non-empty" '[ -s "$DIFF" ]'
_check "artifact-diff-shape" "framework-edit.diff has unified-diff anchors" \
  'grep -qE "^(diff --git|--- |\+\+\+ |@@ )" "$DIFF"'
_check "artifact-verdict-json-exists" "verdict.json exists" '[ -f "$RUN_DIR/verdict.json" ]'
_check "artifact-verdict-json-parses" "verdict.json parses as JSON" \
  'python3 -m json.tool "$RUN_DIR/verdict.json" >/dev/null'
_check "evidence-log-written" "evidence log is writable" '[ -w "$EVIDENCE" ]'

# Task-specific tier.
_check "diff-apply-check-clean" "diff applies cleanly to repo root" \
  'git -C "$REPO_ROOT" apply --check "$DIFF"'
_check "changed-files-extracted" "changed file list can be extracted" \
  '_changed_files && [ -s "$CHANGED" ]'
_check "patched-copy-created" "diff applies to throwaway copy for static checks" \
  '_make_patched_copy'
_check "shell-syntax-clean" "changed shell files pass bash -n" \
  'ok=1; while IFS= read -r f; do case "$f" in *.sh) [ -f "$WORKTREE/$f" ] && bash -n "$WORKTREE/$f" || ok=0;; esac; done <"$CHANGED"; [ "$ok" = 1 ]'
_check "python-compile-clean" "changed Python files compile" \
  'ok=1; while IFS= read -r f; do case "$f" in *.py) [ -f "$WORKTREE/$f" ] && python3 -m py_compile "$WORKTREE/$f" || ok=0;; esac; done <"$CHANGED"; [ "$ok" = 1 ]'
_check "typescript-typecheck-or-explicit-skip" "run typecheck when TS/TSX files changed" \
  'if grep -qE "\.(ts|tsx)$" "$CHANGED"; then if [ -f "$WORKTREE/ui/package.json" ]; then pnpm --dir "$WORKTREE/ui" typecheck; else npm --prefix "$WORKTREE" run typecheck; fi; else echo "skip: no ts/tsx files changed" >&3; fi'
_check "high-blast-radius-guard" "high-blast-radius paths require explicit ALLOW_HIGH_BLAST_RADIUS token" \
  'if grep -qE "^(lib/circuit_breaker\.sh|lib/throttle-guard\.sh|\.mini-ork/config/)" "$CHANGED"; then grep -R -q -E "(^|[^A-Z0-9_])ALLOW_HIGH_BLAST_RADIUS([^A-Z0-9_]|$)" "$RUN_DIR/context-pack.json" "$RUN_DIR/kickoff.md" 2>/dev/null; else true; fi'

python3 - "$NAME" "$EVIDENCE" "$CHECKS_TSV" "$CHANGED" <<'PY'
import json, sys
name, evidence, tsv, changed_path = sys.argv[1:5]
checks = []
with open(tsv) as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"name": cid, "expected": desc, "actual": "see evidence log", "pass": passed == "true"})
failed = [c["name"] for c in checks if not c["pass"]]
changed = [line.strip() for line in open(changed_path) if line.strip()]
print(json.dumps({
    "verifier": name,
    "pass": not failed,
    "verdict": "pass" if not failed else "fail",
    "evidence_path": evidence,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": [f"{c['name']} failed; see {evidence}" for c in checks if not c["pass"]],
    "checked_criteria": [c["name"] for c in checks],
    "artifact_ref": "$MINI_ORK_RUN_DIR/framework-edit.diff",
    "changed_files": changed,
}))
PY

exit 0
