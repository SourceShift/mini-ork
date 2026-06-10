#!/usr/bin/env bash
# verifiers/audit-shape.sh - validate silent-catch-audit recipe shape and outputs.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory (set by mini-ork-execute)
#
# Output: JSON to stdout
#   { "verifier": "audit-shape", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
EVIDENCE="$RUN_DIR/verifier-audit-shape.log"
exec 3>"$EVIDENCE"

checks_run=()
failed_checks=()

_check() {
  local id="$1" expr_desc="$2" cond="$3"
  checks_run+=("$id")
  echo "[$id] $expr_desc" >&3
  if eval "$cond" >&3 2>&1; then
    echo "  ok" >&3
  else
    echo "  FAIL" >&3
    failed_checks+=("$id")
  fi
}

# Template tier: declared recipe artifacts exist and have basic shape.
_check "artifact-workflow-exists" "workflow.yaml exists and is non-empty" \
       '[ -s "$RECIPE_DIR/workflow.yaml" ]'
_check "artifact-contract-exists" "artifact_contract.yaml exists and is non-empty" \
       '[ -s "$RECIPE_DIR/artifact_contract.yaml" ]'
_check "artifact-task-class-exists" "task_class.yaml exists and is non-empty" \
       '[ -s "$RECIPE_DIR/task_class.yaml" ]'
_check "artifact-readme-exists" "README.md exists and is non-empty" \
       '[ -s "$RECIPE_DIR/README.md" ]'
_check "artifact-kickoff-exists" "example-kickoff.md exists and is non-empty" \
       '[ -s "$RECIPE_DIR/example-kickoff.md" ]'
_check "artifact-prompts-exist" "prompts/*.md contains non-empty prompt files" \
       'python3 - "$RECIPE_DIR/prompts" <<'"'"'PY'"'"'
import pathlib, sys
prompt_dir = pathlib.Path(sys.argv[1])
files = sorted(prompt_dir.glob("*.md"))
assert files, "no prompt markdown files"
empty = [str(p.name) for p in files if p.stat().st_size == 0]
assert not empty, "empty prompt files: " + ", ".join(empty)
PY'
_check "artifact-verifiers-exist" "verifiers/*.sh contains this verifier" \
       '[ -s "$RECIPE_DIR/verifiers/audit-shape.sh" ]'
_check "yaml-files-parse" "workflow, artifact contract, and task class parse as YAML" \
       'python3 - "$RECIPE_DIR" <<'"'"'PY'"'"'
import pathlib, sys, yaml
root = pathlib.Path(sys.argv[1])
for name in ("workflow.yaml", "artifact_contract.yaml", "task_class.yaml"):
    data = yaml.safe_load((root / name).read_text())
    assert isinstance(data, dict), f"{name} did not parse to a mapping"
PY'
_check "markdown-shape" "README and kickoff expose expected headings" \
       'grep -qE "^# " "$RECIPE_DIR/README.md" && grep -qE "^# " "$RECIPE_DIR/example-kickoff.md"'

# Task-specific tier: mechanize verifier_contract.checks[] from plan.json.
_check "workflow_yaml_parses" "workflow.yaml declares at least one researcher node" \
       'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
w = yaml.safe_load(open(sys.argv[1]))
nodes = w.get("nodes", [])
assert isinstance(nodes, list), "nodes must be a list"
assert any(n.get("type") == "researcher" for n in nodes), "no researcher node"
PY'

_check "heterogeneity_floor" "workflow has at least three distinct model families" \
       'python3 - "$RECIPE_DIR/workflow.yaml" <<'"'"'PY'"'"'
import sys, yaml
w = yaml.safe_load(open(sys.argv[1]))
families = {
    str(n["model_lane"]).rsplit("_", 1)[0]
    for n in w.get("nodes", [])
    if n.get("model_lane")
}
assert len(families) >= 3, f"only {len(families)} families: {sorted(families)}"
PY'

_check "prompt_refs_resolve" "all workflow prompt_ref entries resolve to non-empty prompts" \
       'python3 - "$RECIPE_DIR" <<'"'"'PY'"'"'
import pathlib, sys, yaml
root = pathlib.Path(sys.argv[1])
w = yaml.safe_load((root / "workflow.yaml").read_text())
missing = []
for node in w.get("nodes", []):
    ref = node.get("prompt_ref")
    if ref:
        path = root / ref
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(ref)
assert not missing, "missing or empty prompt refs: " + ", ".join(missing)
PY'

_check "verifier_refs_resolve" "all workflow verifier_ref entries resolve and pass bash -n" \
       'python3 - "$RECIPE_DIR" <<'"'"'PY'"'"'
import pathlib, subprocess, sys, yaml
root = pathlib.Path(sys.argv[1])
w = yaml.safe_load((root / "workflow.yaml").read_text())
bad = []
for node in w.get("nodes", []):
    ref = node.get("verifier_ref")
    if not ref:
        continue
    path = root / ref
    if not path.is_file() or path.stat().st_size == 0:
        bad.append(f"{ref}: missing or empty")
        continue
    result = subprocess.run(["bash", "-n", str(path)], text=True, capture_output=True)
    if result.returncode != 0:
        bad.append(f"{ref}: bash -n failed: {result.stderr.strip()}")
assert not bad, "; ".join(bad)
PY'

_check "artifact_contract_declares_outputs" "artifact_contract.yaml declares source_artifact and outputs[]" \
       'python3 - "$RECIPE_DIR/artifact_contract.yaml" <<'"'"'PY'"'"'
import sys, yaml
a = yaml.safe_load(open(sys.argv[1]))
assert a.get("source_artifact"), "missing source_artifact"
outputs = a.get("outputs")
assert isinstance(outputs, list) and len(outputs) > 0, "missing outputs"
PY'

_check "task_class_keywords" "task_class.yaml declares at least three match keywords" \
       'python3 - "$RECIPE_DIR/task_class.yaml" <<'"'"'PY'"'"'
import sys, yaml
t = yaml.safe_load(open(sys.argv[1]))
kw = t.get("matches", {}).get("keywords", [])
assert isinstance(kw, list) and len(kw) >= 3, f"only {len(kw)} keywords"
PY'

# Epic-output guards: active only after the recipe has produced run-local outputs.
_check "epic-output-contract" "prompts and contract declare markdown and JSON audit outputs" \
       'grep -R "silent-catch-audit.md" "$RECIPE_DIR" >/dev/null && grep -R "silent-catch-audit.findings.json" "$RECIPE_DIR" >/dev/null'

_check "epic-output-shape-if-present" "if audit outputs exist, markdown/JSON verdict shape is coherent" \
       'python3 - "$RUN_DIR" <<'"'"'PY'"'"'
import json, pathlib, re, sys
run = pathlib.Path(sys.argv[1])
md = run / "silent-catch-audit.md"
js = run / "silent-catch-audit.findings.json"
if not md.exists() and not js.exists():
    raise SystemExit(0)
assert md.is_file() and md.stat().st_size > 0, "missing markdown output"
assert js.is_file() and js.stat().st_size > 0, "missing findings JSON"
text = md.read_text(errors="replace")
assert re.search(r"\b(pass|fail)\b", text, re.I), "markdown missing pass/fail verdict"
for label in ("Critical", "High", "Medium", "Low", "Allowed"):
    assert re.search(rf"\b{label}\b", text), f"markdown missing {label} tier"
data = json.loads(js.read_text())
verdict = data.get("verdict")
assert verdict in {"pass", "fail"}, "JSON verdict must be pass or fail"
summary = data.get("summary", {})
findings = data.get("findings", [])
assert isinstance(summary, dict), "summary must be object"
assert isinstance(findings, list), "findings must be list"
for sev in ("critical", "high", "medium", "low", "allowed"):
    count = summary.get(sev)
    assert isinstance(count, int) and count >= 0, f"summary.{sev} must be non-negative int"
actual = {sev: 0 for sev in ("critical", "high", "medium", "low", "allowed")}
for item in findings:
    sev = str(item.get("severity", "")).lower()
    if sev in actual:
        actual[sev] += 1
for sev, count in actual.items():
    assert summary.get(sev) == count, f"summary.{sev}={summary.get(sev)} but findings have {count}"
assert (verdict == "fail") == (summary.get("critical", 0) > 0), "verdict must fail iff critical > 0"
PY'

_check "read-only-boundary" "recipe does not emit source mutations or lint configuration outputs" \
       'python3 - "$RECIPE_DIR/artifact_contract.yaml" <<'"'"'PY'"'"'
import pathlib, sys, yaml
a = yaml.safe_load(open(sys.argv[1]))
for out in a.get("outputs", []):
    p = pathlib.PurePosixPath(str(out))
    assert p.suffix not in {".ts", ".tsx", ".js", ".jsx"}, f"source output forbidden: {out}"
    assert p.name not in {".eslintrc", ".eslintrc.json", "eslint.config.js", "eslint.config.mjs"}, f"lint config output forbidden: {out}"
PY'

if [ "${#failed_checks[@]}" -eq 0 ]; then
  pass=true
else
  pass=false
fi

python3 - "$pass" "$EVIDENCE" "${checks_run[@]}" -- "${failed_checks[@]}" <<'PY'
import json, sys
pass_value = sys.argv[1] == "true"
evidence_path = sys.argv[2]
sep = sys.argv.index("--")
checks_run = sys.argv[3:sep]
failed_checks = sys.argv[sep + 1:]
print(json.dumps({
    "verifier": "audit-shape",
    "pass": pass_value,
    "evidence_path": evidence_path,
    "checks_run": checks_run,
    "failed_checks": failed_checks,
}))
PY

exit 0
