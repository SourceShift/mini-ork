#!/usr/bin/env bash
# verifiers/recipe-validator.sh — validate the authored framework-edit recipe tree.
#
# Optional arg: recipe directory to validate, used for self-tests.
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MINI_ORK_ROOT:-$(pwd)}"
NAME="recipe-validator"
if [ "${1:-}" ]; then
  NAME="recipe-validator-self-test"
fi
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"
: >"$CHECKS_TSV"
exec 3>"$EVIDENCE"

if [ "${1:-}" ]; then
  RECIPE_DIR="$1"
else
  RECIPE_NAME="$(tr -d '[:space:]' <"$RUN_DIR/chosen/recipe_name" 2>/dev/null || true)"
  RECIPE_DIR="$RUN_DIR/chosen/$RECIPE_NAME"
fi

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

# Template tier: declared recipe artifacts exist and have basic shape.
_check "artifact-workflow-exists" "workflow.yaml exists" '[ -f "$RECIPE_DIR/workflow.yaml" ]'
_check "artifact-workflow-non-empty" "workflow.yaml non-empty" '[ -s "$RECIPE_DIR/workflow.yaml" ]'
_check "artifact-workflow-yaml-parses" "workflow.yaml parses as YAML" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
yaml.safe_load(open(sys.argv[1]))
PY'
_check "artifact-contract-exists" "artifact_contract.yaml exists" '[ -f "$RECIPE_DIR/artifact_contract.yaml" ]'
_check "artifact-contract-yaml-parses" "artifact_contract.yaml parses as YAML" \
  'python3 - "$RECIPE_DIR/artifact_contract.yaml" <<'"'"'PY'"'"'
import sys, yaml
yaml.safe_load(open(sys.argv[1]))
PY'
_check "artifact-task-class-exists" "task_class.yaml exists" '[ -f "$RECIPE_DIR/task_class.yaml" ]'
_check "artifact-task-class-yaml-parses" "task_class.yaml parses as YAML" \
  'python3 - "$RECIPE_DIR/task_class.yaml" <<'"'"'PY'"'"'
import sys, yaml
yaml.safe_load(open(sys.argv[1]))
PY'
_check "prompts-dir-non-empty" "prompts dir has non-empty md files" \
  '[ -d "$RECIPE_DIR/prompts" ] && find "$RECIPE_DIR/prompts" -maxdepth 1 -name "*.md" -type f -size +0c | grep -q .'
_check "verifiers-dir-non-empty" "verifiers dir has shell scripts" \
  '[ -d "$RECIPE_DIR/verifiers" ] && find "$RECIPE_DIR/verifiers" -maxdepth 1 -name "*.sh" -type f | grep -q .'

# Task-specific tier from plan.json verifier_contract.
_check "workflow_yaml_valid" "workflow has at least one researcher node" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
nodes = d.get("nodes") or []
assert any((n.get("node_type") or n.get("type")) == "researcher" for n in nodes)
PY'
_check "family_heterogeneity_floor" "at least three distinct model lanes/families" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import re, sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
def fam(lane):
    m = re.match(r"^([a-z]+)", lane or "")
    return m.group(1) if m else lane
fams = {fam(n.get("model_lane")) for n in d.get("nodes", []) if n.get("model_lane")}
assert len(fams) >= 3
PY'
_check "prompts_exist_nonempty" "every workflow prompt_ref exists and is non-empty" \
  'python3 - "$RECIPE_DIR" <<'"'"'PY'"'"'
import os, sys, yaml
root = sys.argv[1]
wf = yaml.safe_load(open(os.path.join(root, "workflow.yaml"))) or {}
missing = []
for n in wf.get("nodes", []):
    ref = n.get("prompt_ref")
    if ref and ref != "null" and not os.path.getsize(os.path.join(root, ref)):
        missing.append(ref)
assert not missing, missing
PY'
_check "verifiers_exist_executable_syntax_clean" "every workflow verifier_ref exists, is executable, and passes bash -n" \
  'python3 - "$RECIPE_DIR" <<'"'"'PY'"'"'
import os, stat, subprocess, sys, yaml
root = sys.argv[1]
wf = yaml.safe_load(open(os.path.join(root, "workflow.yaml"))) or {}
refs = [n.get("verifier_ref") for n in wf.get("nodes", []) if n.get("verifier_ref")]
assert refs, "no verifier_ref entries"
bad = []
for ref in refs:
    path = os.path.join(root, ref)
    if not os.path.isfile(path) or not os.access(path, os.X_OK) or subprocess.run(["bash", "-n", path]).returncode:
        bad.append(ref)
assert not bad, bad
PY'
_check "artifact_contract_declared" "artifact_contract.yaml declares source_artifact and outputs[]" \
  'python3 - "$RECIPE_DIR/artifact_contract.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
assert d.get("source_artifact") and d.get("outputs")
PY'
_check "task_class_keywords_min3" "task_class.yaml declares at least three matcher keywords" \
  'python3 - "$RECIPE_DIR/task_class.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
assert len(((d.get("matches") or {}).get("keywords") or [])) >= 3
PY'
_check "reviewer_implementer_family_split" "reviewer model lane differs from implementer model lane" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
nodes = d.get("nodes") or []
reviewers = [n.get("model_lane") for n in nodes if (n.get("node_type") or n.get("type")) == "reviewer"]
impls = [n.get("model_lane") for n in nodes if (n.get("node_type") or n.get("type")) == "implementer" and (n.get("id") or n.get("name")) != "verifier_smith"]
assert reviewers and impls and all(r != i for r in reviewers for i in impls)
PY'
_check "verifier_output_contract_nonempty" "static/test verifier outputs have non-empty checks[] and reasons[]" \
  'python3 - "$RUN_DIR" <<'"'"'PY'"'"'
import glob, json, os, sys
run = sys.argv[1]
paths = glob.glob(os.path.join(run, "static-check.json")) + glob.glob(os.path.join(run, "test-verifier.json"))
if not paths:
    raise AssertionError("no verifier output JSON files found")
for path in paths:
    d = json.load(open(path))
    assert d.get("checks"), path
    assert isinstance(d.get("reasons"), list), path
PY'
_check "reviewer_verdict_enum" "review-opus_arbiter.json has verdict approve|revise|reject" \
  'python3 - "$RUN_DIR/review-opus_arbiter.json" <<'"'"'PY'"'"'
import json, sys
assert json.load(open(sys.argv[1])).get("verdict") in {"approve", "revise", "reject"}
PY'
_check "recipe_validator_self_test" "validator can run against known-good recipes/code-fix without NameError" \
  'if [ "$RECIPE_DIR" = "$REPO_ROOT/recipes/code-fix" ] || [ "$RECIPE_DIR" = "recipes/code-fix" ]; then true; else MINI_ORK_RUN_DIR="$RUN_DIR" MINI_ORK_ROOT="$REPO_ROOT" bash "$0" "$REPO_ROOT/recipes/code-fix" >/tmp/framework-edit-recipe-validator-self-test.json && python3 -m json.tool /tmp/framework-edit-recipe-validator-self-test.json >/dev/null; fi'

python3 - "$NAME" "$EVIDENCE" "$CHECKS_TSV" "$RECIPE_DIR" <<'PY'
import json, sys
name, evidence, tsv, recipe_dir = sys.argv[1:5]
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
    "artifact_ref": recipe_dir,
}))
PY

exit 0
