#!/usr/bin/env bash
# verifiers/recipe-validator.sh — single combined validator for the
# meta-recipe-creator. Enforces:
#   1. STRUCTURE  — all required files exist + chmod-able / parseable
#   2. HETEROGENEITY (HARD per user spec) — ≥3 distinct model_lane families
#                    across the chosen recipe's workflow.yaml
#   3. DRY-RUN    — workflow.yaml parses; every prompt_ref / verifier_ref
#                   resolves; every node_type is from the strict 8-element
#                   enum; every edge endpoint exists
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR   run directory (set by mini-ork-execute)
#   MINI_ORK_ROOT      repo root (set by mini-ork wrappers)
#
# Output: single JSON object on stdout
#   { "verifier": "recipe-validator", "pass": bool, "evidence_path": "...",
#     "structure": {...}, "heterogeneity": {...}, "dry_run": {...},
#     "checks_run": [...], "failed_checks": [...] }
# Exit code: always 0 (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}"

EVIDENCE="$RUN_DIR/verifier-recipe-validator.log"
exec 3>"$EVIDENCE"

# Resolve the chosen draft path.
RECIPE_NAME_FILE="$RUN_DIR/chosen/recipe_name"
if [ ! -f "$RECIPE_NAME_FILE" ]; then
  echo "FAIL: chosen/recipe_name missing — arbiter never wrote it" >&3
  python3 -c "import json; print(json.dumps({'verifier':'recipe-validator','pass':False,'evidence_path':'$EVIDENCE','failed_checks':['chosen-recipe-name-missing']}))"
  exit 0
fi
RECIPE_NAME=$(tr -d '[:space:]' < "$RECIPE_NAME_FILE")
CHOSEN_DIR="$RUN_DIR/chosen/$RECIPE_NAME"
if [ ! -d "$CHOSEN_DIR" ]; then
  echo "FAIL: chosen/$RECIPE_NAME/ dir missing" >&3
  python3 -c "import json; print(json.dumps({'verifier':'recipe-validator','pass':False,'evidence_path':'$EVIDENCE','failed_checks':['chosen-dir-missing']}))"
  exit 0
fi

echo "── recipe-validator: chosen=$RECIPE_NAME path=$CHOSEN_DIR ──" >&3

checks_run=()
failed_checks=()

_check() {
  local id="$1" desc="$2" cond="$3"
  checks_run+=("$id")
  echo "  [$id] $desc" >&3
  if eval "$cond" >&3 2>&1; then
    echo "    ok" >&3
    return 0
  else
    echo "    FAIL" >&3
    failed_checks+=("$id")
    return 1
  fi
}

# ── 1. STRUCTURE ──────────────────────────────────────────────────────
_check "name-kebab"        "recipe_name is kebab-case ≤ 32 chars" \
       '[[ "$RECIPE_NAME" =~ ^[a-z][a-z0-9-]{1,31}$ ]] && [[ ! "$RECIPE_NAME" =~ -$ ]]'
_check "workflow-exists"   "workflow.yaml exists" \
       '[ -f "$CHOSEN_DIR/workflow.yaml" ]'
_check "task-class-exists" "task_class.yaml exists" \
       '[ -f "$CHOSEN_DIR/task_class.yaml" ]'
_check "contract-exists"   "artifact_contract.yaml exists" \
       '[ -f "$CHOSEN_DIR/artifact_contract.yaml" ]'
_check "prompts-dir"       "prompts/ dir exists with ≥1 .md" \
       '[ -d "$CHOSEN_DIR/prompts" ] && [ "$(ls "$CHOSEN_DIR/prompts/"*.md 2>/dev/null | wc -l)" -ge 1 ]'
_check "verifiers-dir"     "verifiers/ dir exists with ≥1 .sh" \
       '[ -d "$CHOSEN_DIR/verifiers" ] && [ "$(ls "$CHOSEN_DIR/verifiers/"*.sh 2>/dev/null | wc -l)" -ge 1 ]'
_check "example-kickoff"   "example-kickoff.md exists + non-empty" \
       '[ -s "$CHOSEN_DIR/example-kickoff.md" ]'
_check "readme"            "README.md exists + non-empty" \
       '[ -s "$CHOSEN_DIR/README.md" ]'

# ── 2. DRY-RUN — YAML parses, refs resolve, node_type enum holds ──
DRY_RUN_JSON="$RUN_DIR/recipe-validator-dry-run.json"
python3 - "$CHOSEN_DIR" "$DRY_RUN_JSON" <<'PY' >&3 2>&1
import sys, json, os, re
chosen_dir, out_path = sys.argv[1], sys.argv[2]
res = {"yaml_parses": False, "node_types_valid": False, "refs_resolve": False,
       "edges_endpoints_exist": False, "details": []}
try:
    import yaml
except ImportError:
    print("FAIL: PyYAML not installed")
    res["details"].append("PyYAML missing")
    json.dump(res, open(out_path, "w"))
    sys.exit(0)

VALID_TYPES = {"planner", "researcher", "implementer", "reviewer",
               "verifier", "reflector", "publisher", "rollback"}

try:
    wf = yaml.safe_load(open(os.path.join(chosen_dir, "workflow.yaml")))
    res["yaml_parses"] = True
except Exception as e:
    res["details"].append(f"workflow.yaml parse: {e}")
    json.dump(res, open(out_path, "w"))
    sys.exit(0)

nodes = wf.get("nodes") or []
edges = wf.get("edges") or []
node_names = {n["name"] for n in nodes}

# node_type enum
bad_types = [(n["name"], n.get("type")) for n in nodes if n.get("type") not in VALID_TYPES]
res["node_types_valid"] = not bad_types
if bad_types:
    res["details"].append(f"invalid node_types: {bad_types}")

# prompt_ref / verifier_ref resolve
missing = []
for n in nodes:
    p = n.get("prompt_ref")
    v = n.get("verifier_ref")
    if p and p != "null" and not os.path.exists(os.path.join(chosen_dir, p)):
        missing.append(f"{n['name']}.prompt_ref={p}")
    if v and v != "null" and not os.path.exists(os.path.join(chosen_dir, v)):
        missing.append(f"{n['name']}.verifier_ref={v}")
res["refs_resolve"] = not missing
if missing:
    res["details"].append(f"missing refs: {missing}")

# edges endpoints exist
bad_edges = [e for e in edges if e.get("from") not in node_names or e.get("to") not in node_names]
res["edges_endpoints_exist"] = not bad_edges
if bad_edges:
    res["details"].append(f"orphan edges: {bad_edges}")

json.dump(res, open(out_path, "w"))
print(f"  yaml_parses={res['yaml_parses']} node_types_valid={res['node_types_valid']} refs_resolve={res['refs_resolve']} edges_ok={res['edges_endpoints_exist']}")
PY

_check "dry-run-yaml"       "workflow.yaml parses as YAML" \
       '[ -f "$DRY_RUN_JSON" ] && python3 -c "import json; assert json.load(open(\"$DRY_RUN_JSON\"))[\"yaml_parses\"]"'
_check "dry-run-node-types" "all node_types in strict 8-element enum" \
       '[ -f "$DRY_RUN_JSON" ] && python3 -c "import json; assert json.load(open(\"$DRY_RUN_JSON\"))[\"node_types_valid\"]"'
_check "dry-run-refs"       "every prompt_ref / verifier_ref resolves" \
       '[ -f "$DRY_RUN_JSON" ] && python3 -c "import json; assert json.load(open(\"$DRY_RUN_JSON\"))[\"refs_resolve\"]"'
_check "dry-run-edges"      "every edge.from / edge.to is a declared node" \
       '[ -f "$DRY_RUN_JSON" ] && python3 -c "import json; assert json.load(open(\"$DRY_RUN_JSON\"))[\"edges_endpoints_exist\"]"'

# All verifier stubs must syntax-check via bash -n.
SYNTAX_OK=1
for vf in "$CHOSEN_DIR"/verifiers/*.sh; do
  if ! bash -n "$vf" 2>>"$EVIDENCE"; then
    SYNTAX_OK=0
    echo "    bash -n FAIL: $vf" >&3
  fi
done
_check "verifiers-bash-syntax" "every verifiers/*.sh passes bash -n" \
       '[ "$SYNTAX_OK" = "1" ]'

# ── 3. HETEROGENEITY (HARD — ≥3 distinct families) ────────────────────
HETERO_JSON="$RUN_DIR/recipe-validator-heterogeneity.json"
python3 - "$CHOSEN_DIR" "$HETERO_JSON" "$MINI_ORK_ROOT" <<'PY' >&3 2>&1
import sys, json, os, re
chosen_dir, out_path, mo_root = sys.argv[1], sys.argv[2], sys.argv[3]
res = {"families": {}, "distinct_count": 0, "lanes_used": [], "unresolved_lanes": []}
try:
    import yaml
except ImportError:
    res["unresolved_lanes"].append("PyYAML missing")
    json.dump(res, open(out_path, "w")); sys.exit(0)

# Load chosen recipe's workflow + the live agents.yaml lane → family map.
try:
    wf = yaml.safe_load(open(os.path.join(chosen_dir, "workflow.yaml"))) or {}
except Exception as e:
    res["unresolved_lanes"].append(f"workflow.yaml parse: {e}")
    json.dump(res, open(out_path, "w")); sys.exit(0)

# Find agents.yaml — try .mini-ork/config first, fall back to repo-root config.
agents_paths = [
    os.path.join(os.environ.get("MINI_ORK_HOME", ""), "config", "agents.yaml"),
    os.path.join(mo_root, ".mini-ork", "config", "agents.yaml"),
    os.path.join(mo_root, "config", "agents.yaml"),
]
lane_to_family = {}
for p in agents_paths:
    if p and os.path.exists(p):
        try:
            a = yaml.safe_load(open(p)) or {}
            lane_to_family = a.get("lanes") or {}
            break
        except Exception:
            continue

# Heuristic: lane name itself often encodes family (e.g. glm_lens → glm).
# Fall back to that when agents.yaml lookup is empty.
def family_of(lane):
    if lane in lane_to_family:
        return lane_to_family[lane]
    m = re.match(r"^([a-z]+)(?:_lens|_drafter)?$", lane or "")
    return m.group(1) if m else lane

lanes_used = []
for n in (wf.get("nodes") or []):
    ln = n.get("model_lane")
    if ln:
        lanes_used.append(ln)
res["lanes_used"] = lanes_used

families = {}
unresolved = []
for ln in lanes_used:
    f = family_of(ln)
    if f is None:
        unresolved.append(ln)
        continue
    families[f] = families.get(f, 0) + 1
res["families"] = families
res["distinct_count"] = len(families)
res["unresolved_lanes"] = unresolved
json.dump(res, open(out_path, "w"))
print(f"  distinct_families={res['distinct_count']} families={list(families.keys())}")
PY

_check "heterogeneity-3-families" "≥ 3 distinct model_lane families (HARD floor)" \
       '[ -f "$HETERO_JSON" ] && python3 -c "import json; assert json.load(open(\"$HETERO_JSON\"))[\"distinct_count\"] >= 3"'

# ── Compose verdict ──────────────────────────────────────────────────
# Translate bash truthiness to Python identifiers BEFORE the heredoc so
# the `$pass` expansion inside the Python block becomes a valid literal
# (True/False), not the bash bareword (true/false) that triggers
# NameError: name 'true' is not defined.
if [ "${#failed_checks[@]}" -eq 0 ]; then
  pass=True
else
  pass=False
fi

python3 - <<PY
import json, os
structure = {}
dry = json.load(open("$DRY_RUN_JSON")) if os.path.exists("$DRY_RUN_JSON") else {}
het = json.load(open("$HETERO_JSON")) if os.path.exists("$HETERO_JSON") else {}
out = {
  "verifier": "recipe-validator",
  "pass": $pass,
  "evidence_path": "$EVIDENCE",
  "checks_run": "${checks_run[@]}".split(),
  "failed_checks": "${failed_checks[@]}".split(),
  "dry_run": dry,
  "heterogeneity": het,
}
print(json.dumps(out))
PY

exit 0
