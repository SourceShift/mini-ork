#!/usr/bin/env bash
# verifiers/panel-completeness.sh - verify 4-lens coverage and disagreement math.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory (set by mini-ork-execute)
#
# Output: JSON to stdout
#   { "verifier": "panel-completeness", "pass": bool,
#     "evidence_path": "...", "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-panel-completeness.log"
exec 3>"$EVIDENCE"

SYNTH="$RUN_DIR/chapter-review.json"
LENS_GLM="$RUN_DIR/lens-glm.json"
LENS_KIMI="$RUN_DIR/lens-kimi.json"
LENS_CODEX="$RUN_DIR/lens-codex.json"
LENS_OPUS="$RUN_DIR/lens-opus.json"

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
  python3 - "$1" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    json.load(f)
PY
}

_lens_shape() {
  python3 - "$RUN_DIR" <<'PY'
import json, os, sys

run_dir = sys.argv[1]
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
assigned = {
    "glm": ["C1_structure_flow", "C2_clarity_conciseness", "C7_audience_fit"],
    "kimi": ["C3_style_voice", "C4_engagement_pacing", "C8_narrative_coherence"],
    "codex": ["C5_factuality_citations", "C6_technical_accuracy"],
    "opus": ["C9_originality_insight"],
}
for lens, keys in assigned.items():
    path = os.path.join(run_dir, f"lens-{lens}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("lens") == lens, f"{path}: lens field mismatch"
    axes = data.get("axes")
    assert isinstance(axes, dict), f"{path}: axes must be object"
    assert sorted(axes) == sorted(axis_keys), f"{path}: axes must contain exactly C1..C9"
    for key in axis_keys:
        item = axes[key]
        if key in keys:
            assert isinstance(item, dict), f"{path}: {key} must be scored"
            score = item.get("score")
            conf = item.get("confidence")
            assert isinstance(score, int) and not isinstance(score, bool), f"{path}: {key}.score must be int"
            assert 1 <= score <= 10, f"{path}: {key}.score out of range"
            assert isinstance(item.get("rationale"), str) and item["rationale"].strip(), f"{path}: {key}.rationale empty"
            assert isinstance(conf, (int, float)) and not isinstance(conf, bool), f"{path}: {key}.confidence numeric"
            assert 0 <= conf <= 1, f"{path}: {key}.confidence out of range"
        else:
            assert item is None, f"{path}: unassigned {key} must be null"
    fragments = data.get("fragment_suggestions")
    assert isinstance(fragments, list), f"{path}: fragment_suggestions must be array"
    assessment = data.get("overall_assessment")
    assert isinstance(assessment, str) and assessment.strip(), f"{path}: overall_assessment empty"
PY
}

_synth_references_lenses() {
  python3 - "$RUN_DIR" <<'PY'
import json, os, sys

run_dir = sys.argv[1]
expected = {
    "glm": ["C1_structure_flow", "C2_clarity_conciseness", "C7_audience_fit"],
    "kimi": ["C3_style_voice", "C4_engagement_pacing", "C8_narrative_coherence"],
    "codex": ["C5_factuality_citations", "C6_technical_accuracy"],
    "opus": ["C9_originality_insight"],
}
with open(os.path.join(run_dir, "chapter-review.json"), "r", encoding="utf-8") as f:
    synth = json.load(f)
panel = synth.get("panel")
assert isinstance(panel, dict), "chapter-review.json: panel must be object"
assert sorted(panel) == sorted(expected), "chapter-review.json: panel must contain exactly glm/kimi/codex/opus"
for lens, keys in expected.items():
    with open(os.path.join(run_dir, f"lens-{lens}.json"), "r", encoding="utf-8") as f:
        lens_data = json.load(f)
    panel_scores = panel[lens]
    assert sorted(panel_scores) == sorted(keys), f"panel.{lens} has wrong axis keys"
    for key in keys:
        expected_score = lens_data["axes"][key]["score"]
        actual_score = panel_scores[key]
        assert actual_score == expected_score, f"panel.{lens}.{key} does not match lens score"
PY
}

_disagreement_recomputes() {
  python3 - "$RUN_DIR" <<'PY'
import json, math, os, sys

run_dir = sys.argv[1]
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
lens_docs = []
for lens in ["glm", "kimi", "codex", "opus"]:
    with open(os.path.join(run_dir, f"lens-{lens}.json"), "r", encoding="utf-8") as f:
        lens_docs.append(json.load(f))
with open(os.path.join(run_dir, "chapter-review.json"), "r", encoding="utf-8") as f:
    synth = json.load(f)

norm_vars = []
for key in axis_keys:
    scores = []
    for doc in lens_docs:
        item = doc.get("axes", {}).get(key)
        if isinstance(item, dict) and isinstance(item.get("score"), int):
            scores.append(item["score"])
    if len(scores) < 2:
        norm_vars.append(0.0)
        continue
    mean = sum(scores) / len(scores)
    mean_sq = sum(x * x for x in scores) / len(scores)
    norm_vars.append((mean_sq - mean * mean) / 20.25)
expected = round(sum(norm_vars) / len(axis_keys), 3)
actual = synth.get("panel_disagreement_score")
assert isinstance(actual, (int, float)) and not isinstance(actual, bool), "panel_disagreement_score must be numeric"
assert math.isclose(float(actual), expected, abs_tol=0.001), f"expected {expected}, got {actual}"
PY
}

# Template tier (mechanical) - always.
for artifact in "$LENS_GLM" "$LENS_KIMI" "$LENS_CODEX" "$LENS_OPUS" "$SYNTH"; do
  name="$(basename "$artifact")"
  _check "artifact-exists-$name" "$name exists" '[ -f "$artifact" ]'
  _check "artifact-non-empty-$name" "$name non-empty" '[ -s "$artifact" ]'
  _check "artifact-json-parses-$name" "$name parses as JSON" '_json_parses "$artifact"'
done

# Task-specific tier.
_check "panel-four-lens-shape" "all lens artifacts expose assigned scores and null unassigned axes" '_lens_shape'
_check "panel-synth-references-all-lenses" "chapter-review.json panel references all four lens score groups" '_synth_references_lenses'
_check "panel-disagreement-recomputes" "panel_disagreement_score matches normalized variance formula" '_disagreement_recomputes'

if [ "${#failed_checks[@]}" -eq 0 ]; then
  pass=true
else
  pass=false
fi

PASS_VALUE="$pass" CHECKS_RUN="${checks_run[*]}" FAILED_CHECKS="${failed_checks[*]}" python3 - <<PY
import json, os
print(json.dumps({
    "verifier": "panel-completeness",
    "pass": os.environ.get("PASS_VALUE") == "true",
    "evidence_path": "$EVIDENCE",
    "checks_run": os.environ.get("CHECKS_RUN", "").split(),
    "failed_checks": os.environ.get("FAILED_CHECKS", "").split(),
}))
PY

exit 0
