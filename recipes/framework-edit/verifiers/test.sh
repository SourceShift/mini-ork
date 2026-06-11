#!/usr/bin/env bash
# verifiers/test.sh — apply framework-edit.diff to a copy and run smoke tests.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory set by mini-ork-execute
#   MINI_ORK_ROOT    — optional repo root
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MINI_ORK_ROOT:-$(pwd)}"
NAME="test"
DIFF="$RUN_DIR/framework-edit.diff"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"
WORK_PARENT="$RUN_DIR/verifier-$NAME-work"
WORKTREE="$WORK_PARENT/repo"
: >"$CHECKS_TSV"
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

_make_throwaway_copy() {
  rm -rf "$WORK_PARENT"
  mkdir -p "$WORKTREE"
  git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORKTREE"
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
_check "throwaway-copy-created" "repo HEAD copied under MINI_ORK_RUN_DIR" \
  '_make_throwaway_copy && [ -d "$WORKTREE/tests" ]'
_check "diff-applies-to-copy" "framework-edit.diff applies to throwaway copy" \
  'git -C "$WORKTREE" apply "$DIFF" && touch "$WORK_PARENT/diff-applied.ok"'
_check "web-smoke-test-exists" "tests/test_web_smoke.py exists after patch" \
  '[ -f "$WORKTREE/tests/test_web_smoke.py" ]'
_check "web-smoke-tests-pass" "pytest tests/test_web_smoke.py passes without network keys" \
  '[ -f "$WORK_PARENT/diff-applied.ok" ] && cd "$WORKTREE" && env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u OPENROUTER_API_KEY -u GEMINI_API_KEY PYTHONPATH=. python3 -m pytest tests/test_web_smoke.py -q'

python3 - "$NAME" "$EVIDENCE" "$CHECKS_TSV" <<'PY'
import json, sys
name, evidence, tsv = sys.argv[1:4]
checks = []
with open(tsv) as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"name": cid, "expected": desc, "actual": "see evidence log", "pass": passed == "true"})
failed = [c["name"] for c in checks if not c["pass"]]
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
    "artifact_ref": "$MINI_ORK_RUN_DIR/framework-edit.diff tests/test_web_smoke.py",
}))
PY

exit 0
