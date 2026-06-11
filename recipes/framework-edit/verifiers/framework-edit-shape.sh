#!/usr/bin/env bash
# verifiers/framework-edit-shape.sh — validate the framework-edit recipe tree.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory set by mini-ork-execute
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
NAME="framework-edit-shape"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"
: >"$CHECKS_TSV"
exec 3>"$EVIDENCE"

if [ -d "$RUN_DIR/chosen/framework-edit" ]; then
  RECIPE_DIR="$RUN_DIR/chosen/framework-edit"
elif [ -d "${MINI_ORK_ROOT:-$(pwd)}/recipes/framework-edit" ]; then
  RECIPE_DIR="${MINI_ORK_ROOT:-$(pwd)}/recipes/framework-edit"
else
  RECIPE_DIR="recipes/framework-edit"
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

# Template tier: declared recipe artifacts exist, are non-empty, and parse.
_check "artifact-workflow-exists" "workflow.yaml exists" '[ -f "$RECIPE_DIR/workflow.yaml" ]'
_check "artifact-workflow-non-empty" "workflow.yaml is non-empty" '[ -s "$RECIPE_DIR/workflow.yaml" ]'
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
_check "artifact-example-kickoff-exists" "example-kickoff.md exists and is non-empty" \
  '[ -s "$RECIPE_DIR/example-kickoff.md" ]'
_check "artifact-prompts-non-empty" "prompts/*.md files exist and are non-empty" \
  '[ -d "$RECIPE_DIR/prompts" ] && find "$RECIPE_DIR/prompts" -maxdepth 1 -name "*.md" -type f -size +0c | grep -q .'

# Task-specific tier from plan.json verifier_contract.
_check "workflow_yaml_parses" "workflow has at least one researcher node" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
nodes = d.get("nodes") or []
assert any((n.get("node_type") or n.get("type")) == "researcher" for n in nodes)
PY'
_check "no_meta_node_leakage" "derived workflow contains no meta-recipe node names" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
names = {n.get("id") or n.get("name") for n in d.get("nodes", [])}
forbidden = {"opus_arbiter", "verifier_smith", "glm_drafter", "kimi_drafter", "codex_drafter"}
leaked = sorted(names & forbidden)
assert not leaked, leaked
PY'
_check "exact_nine_nodes" "workflow has exactly 9 nodes" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
assert len(d.get("nodes") or []) == 9
PY'
_check "heterogeneity_floor" "workflow uses at least three distinct model families" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import re, sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
ignored = {"verifier", "publisher", "rollback", "decomposer"}
families = set()
for n in d.get("nodes", []):
    lane = n.get("model_lane")
    if not lane or lane in ignored:
        continue
    families.add((re.match(r"^([a-z]+)", lane) or [lane, lane])[1])
assert len(families) >= 3, families
PY'
_check "lane_bindings_verbatim" "lane assignments match kickoff binding exactly" \
  'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
actual = {n.get("id") or n.get("name"): n.get("model_lane") for n in d.get("nodes", [])}
expected = {
    "planner": "decomposer",
    "code_impact_lens": "kimi_lens",
    "prior_art_lens": "codex_lens",
    "implementer": "glm_lens",
    "static_check_verifier": "verifier",
    "test_verifier": "verifier",
    "reviewer": "opus_lens",
    "publisher": "publisher",
    "rollback": "rollback",
}
assert actual == expected, {"actual": actual, "expected": expected}
PY'
_check "prompts_exist_nonempty" "all referenced prompt_ref files exist and are non-empty" \
  'python3 - "$RECIPE_DIR" <<'"'"'PY'"'"'
import os, sys, yaml
root = sys.argv[1]
wf = yaml.safe_load(open(os.path.join(root, "workflow.yaml"))) or {}
bad = []
for n in wf.get("nodes", []):
    ref = n.get("prompt_ref")
    if ref and ref != "null":
        path = os.path.join(root, ref)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            bad.append(ref)
assert not bad, bad
PY'
_check "verifiers_executable_clean" "referenced verifiers exist and pass bash -n" \
  'python3 - "$RECIPE_DIR" <<'"'"'PY'"'"'
import os, subprocess, sys, yaml
root = sys.argv[1]
wf = yaml.safe_load(open(os.path.join(root, "workflow.yaml"))) or {}
bad = []
for n in wf.get("nodes", []):
    ref = n.get("verifier_ref")
    if not ref:
        continue
    path = os.path.join(root, ref)
    if not os.path.isfile(path) or subprocess.run(["bash", "-n", path]).returncode:
        bad.append(ref)
assert not bad, bad
PY'
_check "artifact_contract_declares_outputs" "artifact_contract declares source_artifact and outputs including required artifacts" \
  'python3 - "$RECIPE_DIR/artifact_contract.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
outs = " ".join(map(str, d.get("outputs") or []))
assert d.get("source_artifact"), d
assert "framework-edit.diff" in outs and "verdict.json" in outs, outs
PY'
_check "task_class_keywords" "task_class.yaml declares at least three keywords" \
  'python3 - "$RECIPE_DIR/task_class.yaml" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1])) or {}
assert len(((d.get("matches") or {}).get("keywords") or [])) >= 3
PY'
_check "verdict_schema_verbatim" "prompts document verdict keys in binding order" \
  'grep -R -qE "files_changed.*tests_pass.*static_pass.*pass" "$RECIPE_DIR/prompts" "$RECIPE_DIR/example-kickoff.md"'
_check "verifier_payload_structured" "review prompt requires structured verifier/reviewer payload fields" \
  'grep -R -q "reasons" "$RECIPE_DIR/prompts" && grep -R -q "checked_criteria" "$RECIPE_DIR/prompts" && grep -R -q "artifact_ref" "$RECIPE_DIR/prompts"'
_check "example_kickoff_exists" "example kickoff describes a realistic two-file mini-ork edit" \
  'grep -qi "two files" "$RECIPE_DIR/example-kickoff.md" && grep -qi "Trajectory" "$RECIPE_DIR/example-kickoff.md"'

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
    "verdict": "pass" if not failed else "fail",
    "evidence_path": evidence,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": [f"{c['name']} failed; see {evidence}" for c in checks if not c["pass"]],
    "checked_criteria": [c["name"] for c in checks],
    "artifact_ref": recipe_dir,
}))
PY

exit 0
