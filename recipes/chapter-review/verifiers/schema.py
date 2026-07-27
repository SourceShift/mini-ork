#!/usr/bin/env python3
# verifiers/schema.py - verify chapter-review.json syntax and strict shape.
#
# Python port of schema.sh (bash-removal WS8). Same checks, evidence text,
# JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "schema", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

import json
import os
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-schema.log")
ev = open(EVIDENCE, "w")

TARGET = os.path.join(RUN_DIR, "chapter-review.json")

checks_run = []
failed_checks = []


def _check(cid, expr_desc, fn):
    checks_run.append(cid)
    ev.write(f"[{cid}] {expr_desc}\n")
    ev.flush()
    try:
        ok = bool(fn())
    except Exception as exc:
        ev.write(f"{type(exc).__name__}: {exc}\n")
        ok = False
    ev.write("  ok\n" if ok else "  FAIL\n")
    ev.flush()
    if not ok:
        failed_checks.append(cid)


def _json_parses():
    with open(TARGET, "r", encoding="utf-8") as f:
        json.load(f)
    return True


def _top_level_shape():
    with open(TARGET, "r", encoding="utf-8") as f:
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
    return True


def _axes_shape():
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
    with open(TARGET, "r", encoding="utf-8") as f:
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
    return True


def _panel_shape():
    expected = {
        "glm": ["C1_structure_flow", "C2_clarity_conciseness", "C7_audience_fit"],
        "kimi": ["C3_style_voice", "C4_engagement_pacing", "C8_narrative_coherence"],
        "codex": ["C5_factuality_citations", "C6_technical_accuracy"],
        "opus": ["C9_originality_insight"],
    }
    with open(TARGET, "r", encoding="utf-8") as f:
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
    return True


def _fragments_shape():
    required = {"fragment", "location", "issue", "fix", "consensus"}
    with open(TARGET, "r", encoding="utf-8") as f:
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
    return True


# Template tier (mechanical) - always.
_check("artifact-exists", "chapter-review.json exists", lambda: os.path.isfile(TARGET))
_check("artifact-non-empty", "chapter-review.json non-empty",
       lambda: os.path.isfile(TARGET) and os.path.getsize(TARGET) > 0)
_check("artifact-json-parses", "chapter-review.json parses as JSON", _json_parses)


def _json_object():
    with open(TARGET, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict), "not an object"
    return True


_check("artifact-json-object", "chapter-review.json top level is an object", _json_object)

# Task-specific tier.
_check("schema-top-level-keys", "required top-level keys and scalar constraints are valid", _top_level_shape)
_check("schema-all-nine-axes", "axes contains canonical C1..C9 objects with score/rationale/confidence/sources", _axes_shape)
_check("schema-panel-four-lenses", "panel contains glm/kimi/codex/opus assigned score groups", _panel_shape)
_check("schema-fragment-suggestions", "fragment_suggestions is an array of actionable fragment objects", _fragments_shape)

passed = not failed_checks

print(json.dumps({
    "verifier": "schema",
    "pass": passed,
    "evidence_path": EVIDENCE,
    "checks_run": checks_run,
    "failed_checks": failed_checks,
}))

ev.close()
sys.exit(0)
