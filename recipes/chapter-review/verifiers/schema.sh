#!/usr/bin/env bash
# verifiers/schema.sh - verify chapter-review.json syntax and strict shape.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "schema", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-schema.log"
exec 3>"$EVIDENCE"

TARGET="$RUN_DIR/chapter-review.json"

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

_json_parses() {
  python3 - "$TARGET" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    json.load(f)
PY
}

_top_level_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

required = {
    "schema_version",
    "chapter_title",
    "panel",
    "axes",
    "fragment_suggestions",
    "overall_verdict",
    "summary",
    "panel_disagreement_score",
}
allowed = set(required) | {"escalation_flag"}
missing = sorted(required - set(data))
extra = sorted(set(data) - allowed)
assert isinstance(data, dict), "top level must be object"
assert not missing, "missing top-level keys: " + ", ".join(missing)
assert not extra, "unexpected top-level keys: " + ", ".join(extra)
assert data["schema_version"] == "1.0.0", "schema_version must be 1.0.0"
assert isinstance(data["chapter_title"], str) and data["chapter_title"].strip(), "chapter_title must be non-empty string"
assert data["overall_verdict"] in {"ACCEPT", "MINOR_REVISION", "MAJOR_REVISION", "REJECT"}, "invalid overall_verdict"
assert isinstance(data["summary"], str) and data["summary"].strip(), "summary must be non-empty string"
score = data["panel_disagreement_score"]
assert isinstance(score, (int, float)) and not isinstance(score, bool), "panel_disagreement_score must be numeric"
assert 0 <= score <= 1, "panel_disagreement_score out of range"
if score > 0.4:
    assert data.get("escalation_flag") is True, "high disagreement requires escalation_flag=true"
elif "escalation_flag" in data:
    assert isinstance(data["escalation_flag"], bool), "escalation_flag must be boolean"
PY
}

_axes_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys

axis_keys = [
    "C1_structure_flow",
    "C2_clarity_conciseness",
    "C3_style_voice",
    "C4_engagement_pacing",
    "C5_factuality_citations",
    "C6_technical_accuracy",
    "C7_audience_fit",
    "C8_narrative_coherence",
    "C9_originality_insight",
]
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
axes = data.get("axes")
assert isinstance(axes, dict), "axes must be object"
assert sorted(axes) == sorted(axis_keys), "axes must contain exactly C1..C9 canonical keys"
for key in axis_keys:
    item = axes[key]
    assert isinstance(item, dict), f"{key} must be object"
    assert sorted(item) == ["confidence", "rationale", "score", "sources"], f"{key} has wrong subkeys"
    assert isinstance(item["score"], int) and not isinstance(item["score"], bool), f"{key}.score must be int"
    assert 1 <= item["score"] <= 10, f"{key}.score out of range"
    assert isinstance(item["rationale"], str) and item["rationale"].strip(), f"{key}.rationale empty"
    conf = item["confidence"]
    assert isinstance(conf, (int, float)) and not isinstance(conf, bool), f"{key}.confidence must be numeric"
    assert 0 <= conf <= 1, f"{key}.confidence out of range"
    assert isinstance(item["sources"], list) and item["sources"], f"{key}.sources must be non-empty array"
    assert all(s in {"glm", "kimi", "codex", "opus"} for s in item["sources"]), f"{key}.sources has invalid lens"
PY
}

_panel_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys

expected = {
    "glm": ["C1_structure_flow", "C2_clarity_conciseness", "C7_audience_fit"],
    "kimi": ["C3_style_voice", "C4_engagement_pacing", "C8_narrative_coherence"],
    "codex": ["C5_factuality_citations", "C6_technical_accuracy"],
    "opus": ["C9_originality_insight"],
}
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
panel = data.get("panel")
assert isinstance(panel, dict), "panel must be object"
assert sorted(panel) == sorted(expected), "panel must contain exactly glm/kimi/codex/opus"
for lens, keys in expected.items():
    item = panel[lens]
    assert isinstance(item, dict), f"panel.{lens} must be object"
    assert sorted(item) == sorted(keys), f"panel.{lens} has wrong axis keys"
    for key in keys:
        value = item[key]
        assert isinstance(value, int) and not isinstance(value, bool), f"panel.{lens}.{key} must be int"
        assert 1 <= value <= 10, f"panel.{lens}.{key} out of range"
PY
}

_fragments_shape() {
  python3 - "$TARGET" <<'PY'
import json, sys

required = {"fragment", "location", "issue", "fix", "consensus"}
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
fragments = data.get("fragment_suggestions")
assert isinstance(fragments, list), "fragment_suggestions must be array"
for i, item in enumerate(fragments):
    assert isinstance(item, dict), f"fragment_suggestions[{i}] must be object"
    missing = required - set(item)
    assert not missing, f"fragment_suggestions[{i}] missing: {sorted(missing)}"
    for key in ["fragment", "location", "issue", "fix"]:
        assert isinstance(item[key], str) and item[key].strip(), f"fragment_suggestions[{i}].{key} empty"
    assert isinstance(item["consensus"], int) and 1 <= item["consensus"] <= 4, f"fragment_suggestions[{i}].consensus out of range"
PY
}

# Template tier (mechanical) - always.
_check "artifact-exists" "chapter-review.json exists" '[ -f "$TARGET" ]'
_check "artifact-non-empty" "chapter-review.json non-empty" '[ -s "$TARGET" ]'
_check "artifact-json-parses" "chapter-review.json parses as JSON" '_json_parses'
_check "artifact-json-object" "chapter-review.json top level is an object" \
       'python3 - "$TARGET" <<'"'"'PY'"'"'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert isinstance(data, dict), "not an object"
PY'

# Task-specific tier.
_check "schema-top-level-keys" "required top-level keys and scalar constraints are valid" '_top_level_shape'
_check "schema-all-nine-axes" "axes contains canonical C1..C9 objects with score/rationale/confidence/sources" '_axes_shape'
_check "schema-panel-four-lenses" "panel contains glm/kimi/codex/opus assigned score groups" '_panel_shape'
_check "schema-fragment-suggestions" "fragment_suggestions is an array of actionable fragment objects" '_fragments_shape'

if [ "${#failed_checks[@]}" -eq 0 ]; then
  pass=true
else
  pass=false
fi

PASS_VALUE="$pass" CHECKS_RUN="${checks_run[*]}" FAILED_CHECKS="${failed_checks[*]}" python3 - <<PY
import json, os
print(json.dumps({
    "verifier": "schema",
    "pass": os.environ.get("PASS_VALUE") == "true",
    "evidence_path": "$EVIDENCE",
    "checks_run": os.environ.get("CHECKS_RUN", "").split(),
    "failed_checks": os.environ.get("FAILED_CHECKS", "").split(),
}))
PY

exit 0
