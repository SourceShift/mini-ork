#!/usr/bin/env bash
# Verify P0 features carry arxiv-search-tool modern techniques references.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:-$PWD}"
NAME="arxiv-compliance-check"
FEATURE_INDEX="$RUN_DIR/feature-index.json"
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

check "feature-index-exists" "feature-index.json exists" '[ -s "$FEATURE_INDEX" ]'
check "p0-modern-techniques-present" "each P0 feature has modern_techniques_refs" \
  'python3 - "$FEATURE_INDEX" <<'"'"'PY'"'"'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
features = data.get("features", data if isinstance(data, list) else [])
p0 = [f for f in features if f.get("priority") == "P0"]
assert p0, "no P0 features found"
missing = []
for feature in p0:
    refs = feature.get("modern_techniques_refs")
    if not isinstance(refs, list) or not refs:
        missing.append(feature.get("id", "<missing-id>"))
assert not missing, f"P0 features missing modern_techniques_refs: {missing}"
PY'
check "p0-arxiv-search-tool-source-present" "each P0 reference names arxiv-search-tool or an arxiv source" \
  'python3 - "$FEATURE_INDEX" <<'"'"'PY'"'"'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
features = data.get("features", data if isinstance(data, list) else [])
bad = []
for feature in features:
    if feature.get("priority") != "P0":
        continue
    refs = feature.get("modern_techniques_refs") or []
    text = json.dumps(refs).lower()
    if "arxiv-search-tool" not in text and "arxiv" not in text:
        bad.append(feature.get("id", "<missing-id>"))
assert not bad, f"P0 features missing arxiv source evidence: {bad}"
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
