#!/usr/bin/env bash
# artifact_contract.sh — ArtifactContract loader and validator.
#
# Public API:
#   artifact_contract_load     <task_class>  → emits contract as JSON on stdout
#   artifact_contract_validate <task_class> <artifact_path>
#                                           → "pass" or "fail" with reasons
#
# Contracts live at:
#   ${MINI_ORK_HOME}/config/artifact_contracts/<task_class>.yaml
#
# Contract schema fields:
#   task_type, expected_artifact (patch|prose|plan|image|data|other),
#   success_verifiers[], failure_policy (request_changes|escalate|rollback),
#   rollback_policy

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_ARTIFACT_CONTRACT_DIR="${MINI_ORK_HOME:-.mini-ork}/config/artifact_contracts"

# desc: Load and return the artifact contract for task_class as JSON.
#       Reads from ${MINI_ORK_HOME}/config/artifact_contracts/<task_class>.yaml.
#       Falls back to a default permissive contract if file not found.
artifact_contract_load() {
  local task_class="${1:?task_class required}"
  local contract_path="${_ARTIFACT_CONTRACT_DIR}/${task_class}.yaml"

  if [[ ! -f "$contract_path" ]]; then
    # Return a default permissive contract
    python3 -c "
import json, sys
tc = sys.argv[1]
print(json.dumps({
    'task_class': tc,
    'task_type': tc,
    'expected_artifact': 'data',
    'success_verifiers': [],
    'failure_policy': 'escalate',
    'rollback_policy': 'none',
    '_default': True,
}))
" "$task_class"
    echo "artifact_contract_load: no contract file at ${contract_path}, using default" >&2
    return 0
  fi

  # Parse YAML to JSON — use python3 with yaml if available, else treat as JSON
  python3 - "$contract_path" "$task_class" <<'PY'
import json, sys, os

path, tc = sys.argv[1], sys.argv[2]

try:
    import yaml
    with open(path) as f:
        data = yaml.safe_load(f)
    data.setdefault("task_class", tc)
    print(json.dumps(data))
except ImportError:
    # yaml not available — try json fallback (user may store .yaml as JSON)
    try:
        with open(path) as f:
            data = json.load(f)
        data.setdefault("task_class", tc)
        print(json.dumps(data))
    except json.JSONDecodeError:
        # Minimal manual YAML key:value parser for simple contracts
        data = {"task_class": tc}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    k, _, v = line.partition(":")
                    k, v = k.strip(), v.strip()
                    if v.startswith("[") or v.startswith("{"):
                        try:
                            data[k] = json.loads(v)
                        except Exception:
                            data[k] = v
                    else:
                        data[k] = v
        print(json.dumps(data))
except Exception as e:
    print(f"artifact_contract_load: error parsing {path}: {e}", file=sys.stderr)
    sys.exit(1)
PY
}

# desc: Validate an artifact at artifact_path against the contract for task_class.
#       Runs each success_verifier[] as a shell command/function with artifact_path
#       as argument. Emits "pass" or "fail\n<reasons_json>" on stdout.
artifact_contract_validate() {
  local task_class="${1:?task_class required}"
  local artifact_path="${2:?artifact_path required}"

  local contract_json
  contract_json="$(artifact_contract_load "$task_class")"

  python3 - "$contract_json" "$artifact_path" <<'PYEOF'
import json, sys, subprocess, os

try:
    contract = json.loads(sys.argv[1])
except json.JSONDecodeError as e:
    print(f"artifact_contract_validate: invalid contract JSON: {e}", file=sys.stderr)
    sys.exit(1)

artifact_path = sys.argv[2]
reasons = []

# Basic existence check
if not os.path.exists(artifact_path):
    reasons.append(f"artifact not found: {artifact_path}")

# Expected artifact type check (simple extension heuristics)
expected = contract.get("expected_artifact", "data")
ext = os.path.splitext(artifact_path)[1].lower()

type_to_exts = {
    "patch": [".patch", ".diff"],
    "prose": [".md", ".txt", ".rst", ".html"],
    "plan":  [".md", ".json", ".yaml", ".yml"],
    "image": [".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"],
    "data":  [".json", ".csv", ".tsv", ".yaml", ".yml", ".ndjson"],
}
allowed_exts = type_to_exts.get(expected, [])
if allowed_exts and ext not in allowed_exts and expected != "other":
    reasons.append(
        f"expected artifact type '{expected}' (extensions {allowed_exts}), "
        f"got '{ext}'"
    )

# Run success_verifiers
verifiers = contract.get("success_verifiers", [])
mini_ork_root = os.environ.get("MINI_ORK_ROOT", ".")

for verifier in verifiers:
    if not isinstance(verifier, str) or not verifier.strip():
        continue
    try:
        # Try as shell command/function with artifact_path as last arg
        cmd = f"source {mini_ork_root}/lib/gate_registry.sh 2>/dev/null; {verifier} '{artifact_path}'"
        proc = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=60,
            env={**os.environ}
        )
        if proc.returncode != 0:
            reasons.append(
                f"verifier '{verifier}' failed (rc={proc.returncode}): "
                f"{proc.stderr.strip()[:120]}"
            )
    except subprocess.TimeoutExpired:
        reasons.append(f"verifier '{verifier}' timed out")
    except Exception as e:
        reasons.append(f"verifier '{verifier}' error: {e}")

if reasons:
    print("fail")
    print(json.dumps({
        "verdict": "fail",
        "reasons": reasons,
        "failure_policy": contract.get("failure_policy", "escalate"),
        "rollback_policy": contract.get("rollback_policy", "none"),
        "task_class": contract.get("task_class", ""),
    }))
else:
    print("pass")
    print(json.dumps({
        "verdict": "pass",
        "task_class": contract.get("task_class", ""),
        "artifact_path": artifact_path,
    }))
PYEOF
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "artifact_contract.sh — source me and call artifact_contract_load / artifact_contract_validate"
fi
