#!/usr/bin/env bash
# verifiers/framework-edit-shape.sh — validate framework-edit output artifacts.
#
# Inputs:
#   MINI_ORK_RUN_DIR — run directory set by mini-ork-execute
#   MINI_ORK_ROOT    — optional repo root for git apply --check
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MINI_ORK_ROOT:-$(pwd)}"
NAME="framework-edit-shape"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"
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

# Template tier: declared artifacts exist, are non-empty, and have basic shape.
_check "artifact-framework-edit-diff-exists" "framework-edit.diff exists" \
  '[ -f "$RUN_DIR/framework-edit.diff" ]'
_check "artifact-framework-edit-diff-non-empty" "framework-edit.diff is non-empty" \
  '[ -s "$RUN_DIR/framework-edit.diff" ]'
_check "artifact-framework-edit-diff-shape" "framework-edit.diff looks like a unified diff" \
  'grep -qE "^(diff --git|--- |\+\+\+ |@@ )" "$RUN_DIR/framework-edit.diff"'
_check "artifact-verdict-json-exists" "verdict.json exists" \
  '[ -f "$RUN_DIR/verdict.json" ]'
_check "artifact-verdict-json-non-empty" "verdict.json is non-empty" \
  '[ -s "$RUN_DIR/verdict.json" ]'
_check "artifact-verdict-json-parses" "verdict.json parses as JSON" \
  'python3 -m json.tool "$RUN_DIR/verdict.json" >/dev/null'
_check "artifact-review-json-exists" "review-opus_arbiter.json exists" \
  '[ -f "$RUN_DIR/review-opus_arbiter.json" ]'
_check "artifact-review-json-non-empty" "review-opus_arbiter.json is non-empty" \
  '[ -s "$RUN_DIR/review-opus_arbiter.json" ]'
_check "artifact-review-json-parses" "review-opus_arbiter.json parses as JSON" \
  'python3 -m json.tool "$RUN_DIR/review-opus_arbiter.json" >/dev/null'

# Task-specific tier.
_check "diff-apply-check-clean" "framework-edit.diff applies cleanly to repo root" \
  'git -C "$REPO_ROOT" apply --check "$RUN_DIR/framework-edit.diff"'
_check "verdict-required-schema" "verdict.json has required typed keys" \
  'python3 - "$RUN_DIR/verdict.json" <<'"'"'PY'"'"'
import json, sys
d = json.load(open(sys.argv[1]))
assert isinstance(d.get("files_changed"), int)
for k in ("tests_pass", "static_pass", "pass"):
    assert isinstance(d.get(k), bool)
PY'
_check "verdict-pass-derived" "verdict.pass equals tests_pass && static_pass" \
  'python3 - "$RUN_DIR/verdict.json" <<'"'"'PY'"'"'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["pass"] == (d["tests_pass"] and d["static_pass"])
PY'
_check "reviewer-verdict-enum" "review-opus_arbiter.json verdict is approve|revise|reject" \
  'python3 - "$RUN_DIR/review-opus_arbiter.json" <<'"'"'PY'"'"'
import json, sys
assert json.load(open(sys.argv[1])).get("verdict") in {"approve", "revise", "reject"}
PY'

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
    "evidence_path": evidence,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": [f"{c['name']} failed; see {evidence}" for c in checks if not c["pass"]],
    "artifact_ref": "$MINI_ORK_RUN_DIR/framework-edit.diff verdict.json review-opus_arbiter.json",
}))
PY

exit 0
