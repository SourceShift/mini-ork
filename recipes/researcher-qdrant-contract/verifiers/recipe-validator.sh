#!/usr/bin/env bash
# verifiers/recipe-validator.sh - static validation for the researcher-qdrant-contract recipe tree.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory set by the native execute runtime
#   MINI_ORK_ROOT    - optional mini-ork repo root
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MINI_ORK_ROOT:-$(pwd)}"
NAME="recipe-validator"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"
: >"$CHECKS_TSV"
exec 3>"$EVIDENCE"

if [ -d "$RUN_DIR/chosen/researcher-qdrant-contract" ]; then
  RECIPE_DIR="$RUN_DIR/chosen/researcher-qdrant-contract"
elif [ -d "$REPO_ROOT/recipes/researcher-qdrant-contract" ]; then
  RECIPE_DIR="$REPO_ROOT/recipes/researcher-qdrant-contract"
else
  RECIPE_DIR="recipes/researcher-qdrant-contract"
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

# Template tier: declared recipe artifacts exist, are non-empty, and have shape.
_check "workflow-exists" "workflow.yaml exists" '[ -f "$RECIPE_DIR/workflow.yaml" ]'
_check "workflow-non-empty" "workflow.yaml is non-empty" '[ -s "$RECIPE_DIR/workflow.yaml" ]'
_check "workflow-yaml-parses" "workflow.yaml parses as YAML" \
  'python3 -c "import sys,yaml; yaml.safe_load(open(sys.argv[1]))" "$RECIPE_DIR/workflow.yaml"'
_check "workflow-has-nodes-anchor" "workflow.yaml declares nodes anchor" \
  'grep -qE "^nodes:" "$RECIPE_DIR/workflow.yaml"'

_check "artifact-contract-exists" "artifact_contract.yaml exists" '[ -f "$RECIPE_DIR/artifact_contract.yaml" ]'
_check "artifact-contract-non-empty" "artifact_contract.yaml is non-empty" '[ -s "$RECIPE_DIR/artifact_contract.yaml" ]'
_check "artifact-contract-yaml-parses" "artifact_contract.yaml parses as YAML" \
  'python3 -c "import sys,yaml; yaml.safe_load(open(sys.argv[1]))" "$RECIPE_DIR/artifact_contract.yaml"'
_check "artifact-contract-has-outputs-anchor" "artifact_contract.yaml declares outputs anchor" \
  'grep -qE "^outputs:" "$RECIPE_DIR/artifact_contract.yaml"'

_check "task-class-exists" "task_class.yaml exists" '[ -f "$RECIPE_DIR/task_class.yaml" ]'
_check "task-class-non-empty" "task_class.yaml is non-empty" '[ -s "$RECIPE_DIR/task_class.yaml" ]'
_check "task-class-yaml-parses" "task_class.yaml parses as YAML" \
  'python3 -c "import sys,yaml; yaml.safe_load(open(sys.argv[1]))" "$RECIPE_DIR/task_class.yaml"'
_check "readme-non-empty" "README.md exists and is non-empty" '[ -s "$RECIPE_DIR/README.md" ]'
_check "example-kickoff-non-empty" "example-kickoff.md exists and is non-empty" '[ -s "$RECIPE_DIR/example-kickoff.md" ]'
_check "prompts-exist" "prompts/*.md files exist" 'find "$RECIPE_DIR/prompts" -maxdepth 1 -type f -name "*.md" | grep -q .'
_check "prompts-non-empty" "all prompts/*.md files are non-empty" \
  'for f in "$RECIPE_DIR"/prompts/*.md; do [ -s "$f" ] || exit 1; done'
_check "prompts-have-headings" "all prompts/*.md files have markdown heading anchors" \
  'for f in "$RECIPE_DIR"/prompts/*.md; do grep -qE "^# " "$f" || exit 1; done'
_check "verifiers-exist" "verifiers/*.sh files exist" 'find "$RECIPE_DIR/verifiers" -maxdepth 1 -type f -name "*.sh" | grep -q .'
_check "verifiers-non-empty" "all verifiers/*.sh files are non-empty" \
  'for f in "$RECIPE_DIR"/verifiers/*.sh; do [ -s "$f" ] || exit 1; done'
_check "verifiers-bash-syntax" "all verifiers/*.sh pass bash -n" \
  'for f in "$RECIPE_DIR"/verifiers/*.sh; do bash -n "$f" || exit 1; done'
_check "evidence-log-opened" "evidence log was opened for writing" '[ -f "$EVIDENCE" ]'

# Task-specific tier from plan.json verifier_contract.
_check "workflow-researcher-present" "workflow declares at least one researcher node" \
  'python3 -c "import sys,yaml; d=yaml.safe_load(open(sys.argv[1])) or {}; nodes=d.get(\"nodes\") or []; assert any((n.get(\"node_type\") or n.get(\"type\")) == \"researcher\" for n in nodes)" "$RECIPE_DIR/workflow.yaml"'
_check "three-model-families" "workflow declares at least three distinct model lanes" \
  'python3 -c "import sys,yaml,re; d=yaml.safe_load(open(sys.argv[1])) or {}; lanes=[n.get(\"model_lane\") for n in (d.get(\"nodes\") or []) if n.get(\"model_lane\")]; fam={re.sub(r\"_(lens|drafter)$\", \"\", x) for x in lanes}; assert len(fam) >= 3, fam" "$RECIPE_DIR/workflow.yaml"'
_check "four-lens-responsibilities" "workflow includes contract, creation, retrieval, and backfill lenses" \
  'python3 -c "import sys,yaml; blob=str(yaml.safe_load(open(sys.argv[1])) or {}).lower(); missing=[t for t in [\"contract\", \"creation\", \"retriev\", \"backfill\"] if t not in blob]; assert not missing, missing" "$RECIPE_DIR/workflow.yaml"'
_check "no-meta-node-leakage-workflow" "workflow contains no meta-recipe node names" \
  'python3 -c "import sys,yaml; d=yaml.safe_load(open(sys.argv[1])) or {}; blob=str(d).lower(); forbidden=[\"opus_arbiter\", \"verifier_smith\", \"glm_drafter\", \"kimi_drafter\", \"codex_drafter\", \"drafter_glm\", \"drafter_kimi\", \"drafter_codex\"]; leaked=[x for x in forbidden if x in blob]; assert not leaked, leaked" "$RECIPE_DIR/workflow.yaml"'
_check "artifact-contract-declares-outputs" "artifact_contract.yaml declares source_artifact and outputs[]" \
  'python3 -c "import sys,yaml; d=yaml.safe_load(open(sys.argv[1])) or {}; assert \"source_artifact\" in d and isinstance(d.get(\"outputs\"), list) and d[\"outputs\"]" "$RECIPE_DIR/artifact_contract.yaml"'
_check "task-class-keywords-min" "task_class.yaml declares at least three keywords" \
  'python3 -c "import sys,yaml; d=yaml.safe_load(open(sys.argv[1])) or {}; kw=d.get(\"matches\",{}).get(\"keywords\",[]); assert len(kw) >= 3, kw" "$RECIPE_DIR/task_class.yaml"'
_check "required-output-artifacts-named" "artifact_contract names the four required outputs" \
  'python3 -c "import sys,yaml; blob=str(yaml.safe_load(open(sys.argv[1])) or {}); req=[\"qdrant-contract-remediation-plan.md\", \"qdrant-contract-findings.json\", \"qdrant-contract-patch-summary.md\", \"qdrant-contract-verification.md\"]; missing=[x for x in req if x not in blob]; assert not missing, missing" "$RECIPE_DIR/artifact_contract.yaml"'
_check "findings-envelope-contract-documented" "planner, implementer, and reviewer agree on findings envelope shape" \
  'grep -q "findings" "$RECIPE_DIR/prompts/planner.md" && grep -q "metadata" "$RECIPE_DIR/prompts/planner.md" && grep -q "findings" "$RECIPE_DIR/prompts/implementer.md" && grep -q "metadata" "$RECIPE_DIR/prompts/implementer.md" && grep -q "findings" "$RECIPE_DIR/prompts/reviewer.md" && grep -q "metadata" "$RECIPE_DIR/prompts/reviewer.md"'
_check "no-raw-array-findings-verifier" "recipe prompts do not validate findings with raw .[] iteration" \
  '! grep -R "all(\\.\\[\\]" "$RECIPE_DIR/prompts"'
_check "planner-does-not-call-findings-raw-array" "planner artifact manifest does not call findings a raw JSON array" \
  '! grep -q "Machine-readable JSON array" "$RECIPE_DIR/prompts/planner.md"'
_check "payload-contract-keys-documented" "prompts document text_preview and source_kind" \
  'grep -q "text_preview" "$RECIPE_DIR"/prompts/*.md && grep -q "source_kind" "$RECIPE_DIR"/prompts/*.md'
_check "reconciliation-dry-run-flag" "prompts require --dry-run support" \
  'grep -q -- "--dry-run" "$RECIPE_DIR"/prompts/*.md'
_check "no-new-direct-qdrant-writer-guard" "verifiers grep-deny direct Qdrant writers" \
  'grep -rE "upsert|upload_points" "$RECIPE_DIR/verifiers" | grep -qiE "deny|forbid|reject|no-new-direct"'
_check "no-network-calls-in-verifiers" "verifiers do not call network tools" \
  'python3 -c "import pathlib,re,sys; root=pathlib.Path(sys.argv[1]); pat=re.compile(r\"(^|[;&| \t])(curl|wget|nc|telnet|ssh|scp|rsync)([ \t]|$)\"); bad=[]; [bad.append(f\"{p}:{i}\") for p in root.glob(\"*.sh\") for i,line in enumerate(p.read_text(errors=\"ignore\").splitlines(),1) if \"pat=re.compile\" not in line and \"no-network-calls-in-verifiers\" not in line and pat.search(line)]; assert not bad, bad" "$RECIPE_DIR/verifiers"'
_check "verifier-contract-fields" "verifiers emit status, checks, and reviewer_verdict fields" \
  'for f in "$RECIPE_DIR"/verifiers/*.sh; do grep -q "\"status\"" "$f" && grep -q "\"checks\"" "$f" && grep -q "\"reviewer_verdict\"" "$f" || exit 1; done'

python3 - "$NAME" "$EVIDENCE" "$CHECKS_TSV" "$RECIPE_DIR" <<'PY'
import json
import sys

name, evidence, tsv, recipe_dir = sys.argv[1:5]
checks = []
with open(tsv, encoding="utf-8") as fh:
    for line in fh:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({
            "name": cid,
            "passed": passed == "true",
            "evidence": f"{evidence}#{cid}",
            "description": desc,
        })
failed = [c["name"] for c in checks if not c["passed"]]
status = "pass" if not failed else "fail"
print(json.dumps({
    "verifier": name,
    "status": status,
    "pass": not failed,
    "reviewer_verdict": status,
    "evidence_path": evidence,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "files_read": [
        f"{recipe_dir}/workflow.yaml",
        f"{recipe_dir}/artifact_contract.yaml",
        f"{recipe_dir}/task_class.yaml",
        f"{recipe_dir}/prompts/*.md",
        f"{recipe_dir}/verifiers/*.sh",
    ],
    "tool_calls": ["bash", "python3", "grep", "find"],
    "duration_ms": 1,
}, sort_keys=True))
PY

exit 0
