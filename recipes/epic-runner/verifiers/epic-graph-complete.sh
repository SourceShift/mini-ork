#!/usr/bin/env bash
# verifiers/epic-graph-complete.sh — validate epic-runner recipe shape and run artifacts.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "epic-graph-complete", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
NAME="epic-graph-complete"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"
START_MS="$(python3 -c 'import time; print(int(time.time() * 1000))')"

: >"$CHECKS_TSV"
exec 3>"$EVIDENCE"

if [ -d "$RUN_DIR/chosen/epic-runner" ]; then
  RECIPE_DIR="$RUN_DIR/chosen/epic-runner"
elif [ -d "$RUN_DIR/recipes/epic-runner" ]; then
  RECIPE_DIR="$RUN_DIR/recipes/epic-runner"
elif [ -d "$PWD/recipes/epic-runner" ]; then
  RECIPE_DIR="$PWD/recipes/epic-runner"
else
  RECIPE_DIR="$RUN_DIR/chosen/epic-runner"
fi

WORKFLOW="$RECIPE_DIR/workflow.yaml"
TASK_CLASS="$RECIPE_DIR/task_class.yaml"
ARTIFACT_CONTRACT="$RECIPE_DIR/artifact_contract.yaml"
README="$RECIPE_DIR/README.md"
EXAMPLE="$RECIPE_DIR/example-kickoff.md"
SELF="$RECIPE_DIR/verifiers/epic-graph-complete.sh"
PLAN="$RUN_DIR/epic-runner-plan.json"
RESULTS="$RUN_DIR/epic-results.json"
AGGREGATE="$RUN_DIR/wave-aggregate.json"
DELIVERY="$RUN_DIR/epic-runner-delivery.json"
TRACE="${MINI_ORK_TRACE_PATH:-$RUN_DIR/trace.json}"

if [ -n "${MINI_ORK_HOME:-}" ] && [ -f "$MINI_ORK_HOME/config/agents.yaml" ]; then
  AGENTS="$MINI_ORK_HOME/config/agents.yaml"
elif [ -f "$RUN_DIR/.mini-ork/config/agents.yaml" ]; then
  AGENTS="$RUN_DIR/.mini-ork/config/agents.yaml"
elif [ -f "$PWD/config/agents.yaml" ]; then
  AGENTS="$PWD/config/agents.yaml"
else
  AGENTS=""
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

# Template tier (mechanical) — always.
_check "artifact-exists" "declared recipe artifacts exist" \
  '[ -f "$WORKFLOW" ] && [ -f "$TASK_CLASS" ] && [ -f "$ARTIFACT_CONTRACT" ] && [ -f "$README" ] && [ -f "$EXAMPLE" ] && [ -f "$SELF" ]'
_check "artifact-non-empty" "declared recipe artifacts are non-empty" \
  '[ -s "$WORKFLOW" ] && [ -s "$TASK_CLASS" ] && [ -s "$ARTIFACT_CONTRACT" ] && [ -s "$README" ] && [ -s "$EXAMPLE" ] && [ -s "$SELF" ]'
_check "yaml-shape" "workflow/task_class/artifact_contract parse as YAML" \
  'python3 - "$WORKFLOW" "$TASK_CLASS" "$ARTIFACT_CONTRACT" <<'"'"'PY'"'"'
import sys, yaml
for path in sys.argv[1:]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"{path} did not parse to a mapping"
PY'
_check "markdown-shape" "README and example have useful line count and anchors" \
  'python3 - "$README" "$EXAMPLE" <<'"'"'PY'"'"'
import re, sys
for path in sys.argv[1:]:
    text = open(path, encoding="utf-8").read()
    assert len(text.splitlines()) >= 10, f"{path} is too short"
assert "MINI_ORK_EPIC_DOC" in open(sys.argv[1], encoding="utf-8").read()
assert re.search(r"(researcher|schema-migration|scalable-schema-migration)", open(sys.argv[2], encoding="utf-8").read())
PY'
_check "prompt-artifacts-shape" "required prompt artifacts exist and are non-empty" \
  'for f in planner epic-dispatcher wave-aggregator final-reviewer; do [ -s "$RECIPE_DIR/prompts/$f.md" ] || exit 1; done'
_check "verifier-bash-n" "verifier script passes bash -n" \
  'bash -n "$SELF"'
_check "evidence-log-opened" "evidence log path is writable" \
  '[ -s "$EVIDENCE" ]'

# Task-specific tier (plan verifier_contract + epic-runner semantics).
_check "workflow-yaml-parses" "workflow.yaml exists and parses as valid YAML" \
  'python3 -c "import yaml; yaml.safe_load(open(\"$WORKFLOW\"))"'
_check "workflow-node-count-exact-8" "workflow.yaml declares exactly 8 nodes" \
  'python3 - "$WORKFLOW" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
nodes = d.get("nodes", [])
assert len(nodes) == 8, f"expected 8 got {len(nodes)}"
PY'
_check "workflow-binding-shape" "workflow node names match the kickoff binding exactly" \
  'python3 - "$WORKFLOW" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
names = {n.get("name") for n in d.get("nodes", [])}
expected = {"planner", "epic_dispatcher", "wave_aggregator", "epic_verifier", "final_reviewer", "publisher", "rollback", "reflector"}
assert names == expected, f"missing={sorted(expected-names)} extra={sorted(names-expected)}"
PY'
_check "no-meta-recipe-leakage" "recipe files do not contain recipe-creator node names" \
  '! grep -R --exclude="epic-graph-complete.sh" -E "(arxiv_lens|prior_art_lens|glm_drafter|kimi_drafter|codex_drafter|verifier_smith|opus_arbiter|recipe_validator)" "$RECIPE_DIR"'
_check "heterogeneity-3-families" "at least 3 distinct model families across non-operational model lanes" \
  'python3 - "$WORKFLOW" "$AGENTS" <<'"'"'PY'"'"'
import sys, yaml
workflow, agents_path = sys.argv[1], sys.argv[2]
d = yaml.safe_load(open(workflow))
lanes = {}
if agents_path:
    agents = yaml.safe_load(open(agents_path)) or {}
    lanes = agents.get("lanes", {}) or {}
operational = {"planner", "verifier", "publisher", "rollback", "reflector"}
families = set()
for node in d.get("nodes", []):
    lane = node.get("model_lane")
    if not lane or lane in operational:
        continue
    family = lanes.get(lane, lane)
    if family not in operational:
        families.add(str(family))
assert len(families) >= 3, f"expected >=3 families got {sorted(families)}"
PY'
_check "prompts-files-exist-nonempty" "required prompt files exist and are non-empty" \
  'for f in planner epic-dispatcher wave-aggregator final-reviewer; do test -s "$RECIPE_DIR/prompts/$f.md" || { echo "missing or empty: $f.md"; exit 1; }; done'
_check "verifier-script-exists-and-bash-n-clean" "epic-graph-complete.sh exists and passes bash -n" \
  'test -s "$SELF" && bash -n "$SELF"'
_check "artifact-contract-declares-empty-outputs" "artifact_contract.yaml declares outputs: []" \
  'python3 - "$ARTIFACT_CONTRACT" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
assert d.get("outputs") == [], f"expected outputs: [] got {d.get('outputs')}"
PY'
_check "task-class-keywords-present" "task_class.yaml includes all kickoff-required keywords" \
  'python3 - "$TASK_CLASS" <<'"'"'PY'"'"'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
kw = set(d["matches"]["keywords"])
req = {"deliver epic", "multi-epic", "schema migration", "epic runner", "epic doc"}
assert req <= kw, f"missing keywords: {sorted(req-kw)}"
PY'
_check "example-kickoff-references-researcher-doc" "example kickoff references researcher schema-migration use case" \
  'grep -qE "(researcher|schema-migration|scalable-schema-migration)" "$EXAMPLE"'
_check "readme-documents-env-vars" "README documents all epic-runner env vars" \
  'for v in MINI_ORK_EPIC_DOC MINI_ORK_EPIC_TARGET_REPO MINI_ORK_EPIC_PUBLISH MINI_ORK_EPIC_VERIFIER_SCRIPT; do grep -q "$v" "$README" || { echo "missing env var docs: $v"; exit 1; }; done'
_check "publisher-evidence-non-empty" "publisher evidence is non-empty when MINI_ORK_EPIC_PUBLISH=true" \
  'python3 - "$TRACE" "${MINI_ORK_EPIC_PUBLISH:-false}" <<'"'"'PY'"'"'
import json, os, sys
trace, publish = sys.argv[1], sys.argv[2].lower()
if publish != "true":
    raise SystemExit(0)
assert os.path.exists(trace), f"trace not found: {trace}"
t = json.load(open(trace))
nodes = t.get("nodes", [])
pub = next((n for n in nodes if n.get("name") == "publisher"), None)
assert pub, "publisher node missing from trace"
assert pub.get("final_artifact_ref") and pub.get("files_written"), "publisher emitted empty evidence"
PY'
_check "runtime-json-artifacts-parse-if-present" "runtime JSON artifacts parse when present" \
  'python3 - "$PLAN" "$RESULTS" "$AGGREGATE" "$DELIVERY" <<'"'"'PY'"'"'
import json, os, sys
for path in sys.argv[1:]:
    if os.path.exists(path):
        json.load(open(path))
PY'
_check "runtime-all-epics-represented-if-present" "every planned epic appears in results when runtime artifacts exist" \
  'python3 - "$PLAN" "$RESULTS" <<'"'"'PY'"'"'
import json, os, sys
plan_path, results_path = sys.argv[1:3]
if not (os.path.exists(plan_path) and os.path.exists(results_path)):
    raise SystemExit(0)
plan = json.load(open(plan_path))
results = json.load(open(results_path))
planned = {e["id"] for e in plan.get("epics", [])}
found = {e["id"] for e in results.get("epics", [])}
missing = planned - found
assert not missing, f"missing epics: {sorted(missing)}"
PY'
_check "runtime-dependency-respected-if-present" "wave aggregate reports dependency_respected=true when present" \
  'python3 - "$AGGREGATE" <<'"'"'PY'"'"'
import json, os, sys
path = sys.argv[1]
if not os.path.exists(path):
    raise SystemExit(0)
d = json.load(open(path))
assert d.get("aggregate", {}).get("dependency_respected") is True
PY'

END_MS="$(python3 -c 'import time; print(int(time.time() * 1000))')"
DURATION_MS=$((END_MS - START_MS))

python3 - "$NAME" "$EVIDENCE" "$CHECKS_TSV" "$RECIPE_DIR" "$DURATION_MS" <<'PY'
import json, sys
name, evidence, checks_tsv, recipe_dir, duration_ms = sys.argv[1:6]
checks = []
with open(checks_tsv, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        cid, desc, passed = line.split("\t", 2)
        checks.append({"id": cid, "description": desc, "pass": passed == "true"})
failed = [c["id"] for c in checks if not c["pass"]]
print(json.dumps({
    "verifier": name,
    "pass": not failed,
    "evidence_path": evidence,
    "checks_run": [c["id"] for c in checks],
    "failed_checks": failed,
    "checked_criteria": [c["id"] for c in checks],
    "artifact_ref": recipe_dir,
    "duration_ms": int(duration_ms),
}, sort_keys=True))
PY

exit 0
