#!/usr/bin/env bash
# Validate that feature-index.json contains enough extracted features.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:-$PWD}"
NAME="extraction-completeness"
FEATURE_INDEX="$RUN_DIR/feature-index.json"
MIN_FEATURES="${MO_DOC_LOOP_MIN_FEATURES:-5}"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
CHECKS_TSV="$RUN_DIR/verifier-$NAME.checks.tsv"

: >"$CHECKS_TSV"
exec 3>"$EVIDENCE"

check() {
  local id="$1" desc="$2" cond="$3"
  echo "[$id] $desc" >&3
  if eval "$cond" >&3 2>&1; then
    printf '%s\t%s\ttrue\n' "$id" "$desc" >>"$CHECKS_TSV"
  else
    printf '%s\t%s\tfalse\n' "$id" "$desc" >>"$CHECKS_TSV"
  fi
}

check "feature-index-exists" "feature-index.json exists and is non-empty" \
  '[ -s "$FEATURE_INDEX" ]'
check "feature-index-json-valid" "feature-index.json parses as JSON" \
  'python3 - "$FEATURE_INDEX" <<'"'"'PY'"'"'
import json, sys
json.load(open(sys.argv[1], encoding="utf-8"))
PY'
check "feature-count-minimum" "feature count is at least MO_DOC_LOOP_MIN_FEATURES" \
  'python3 - "$FEATURE_INDEX" "$MIN_FEATURES" <<'"'"'PY'"'"'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
features = data.get("features", data if isinstance(data, list) else [])
assert isinstance(features, list), "features must be a list"
minimum = int(sys.argv[2])
assert len(features) >= minimum, f"expected >= {minimum} features, got {len(features)}"
PY'
check "feature-required-fields" "each feature has id, title, priority, and dependencies" \
  'python3 - "$FEATURE_INDEX" <<'"'"'PY'"'"'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
features = data.get("features", data if isinstance(data, list) else [])
for i, feature in enumerate(features):
    assert feature.get("id"), f"feature {i} missing id"
    assert feature.get("title"), f"{feature.get('id', i)} missing title"
    assert feature.get("priority"), f"{feature.get('id', i)} missing priority"
    assert isinstance(feature.get("dependencies", []), list), f"{feature.get('id', i)} dependencies must be list"
PY'

python3 - "$NAME" "$EVIDENCE" "$CHECKS_TSV" "$FEATURE_INDEX" <<'PY'
import json, sys
name, evidence, checks_tsv, feature_index = sys.argv[1:5]
checks = []
with open(checks_tsv, encoding="utf-8") as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"id": cid, "description": desc, "pass": passed == "true"})
failed = [c["id"] for c in checks if not c["pass"]]
print(json.dumps({
    "verifier": name,
    "pass": not failed,
    "evidence_path": evidence,
    "checks_run": [c["id"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": failed,
    "artifact_ref": feature_index
}))
PY
