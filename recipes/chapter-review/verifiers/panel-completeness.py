#!/usr/bin/env python3
# verifiers/panel-completeness.py - verify 4-lens coverage and disagreement math.
#
# Python port of panel-completeness.sh (bash-removal WS8). Same checks,
# evidence text, JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "panel-completeness", "pass": bool,
#     "evidence_path": "...", "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

import json
import math
import os
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-panel-completeness.log")
ev = open(EVIDENCE, "w")

SYNTH = os.path.join(RUN_DIR, "chapter-review.json")
LENS_GLM = os.path.join(RUN_DIR, "lens-glm.json")
LENS_KIMI = os.path.join(RUN_DIR, "lens-kimi.json")
LENS_CODEX = os.path.join(RUN_DIR, "lens-codex.json")
LENS_OPUS = os.path.join(RUN_DIR, "lens-opus.json")

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


def _lens_shape():
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
        path = os.path.join(RUN_DIR, f"lens-{lens}.json")
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
    return True


def _synth_references_lenses():
    expected = {
        "glm": ["C1_structure_flow", "C2_clarity_conciseness", "C7_audience_fit"],
        "kimi": ["C3_style_voice", "C4_engagement_pacing", "C8_narrative_coherence"],
        "codex": ["C5_factuality_citations", "C6_technical_accuracy"],
        "opus": ["C9_originality_insight"],
    }
    with open(os.path.join(RUN_DIR, "chapter-review.json"), "r", encoding="utf-8") as f:
        synth = json.load(f)
    panel = synth.get("panel")
    assert isinstance(panel, dict), "chapter-review.json: panel must be object"
    assert sorted(panel) == sorted(expected), "chapter-review.json: panel must contain exactly glm/kimi/codex/opus"
    for lens, keys in expected.items():
        with open(os.path.join(RUN_DIR, f"lens-{lens}.json"), "r", encoding="utf-8") as f:
            lens_data = json.load(f)
        panel_scores = panel[lens]
        assert sorted(panel_scores) == sorted(keys), f"panel.{lens} has wrong axis keys"
        for key in keys:
            expected_score = lens_data["axes"][key]["score"]
            actual_score = panel_scores[key]
            assert actual_score == expected_score, f"panel.{lens}.{key} does not match lens score"
    return True


def _disagreement_recomputes():
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
        with open(os.path.join(RUN_DIR, f"lens-{lens}.json"), "r", encoding="utf-8") as f:
            lens_docs.append(json.load(f))
    with open(os.path.join(RUN_DIR, "chapter-review.json"), "r", encoding="utf-8") as f:
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
    return True


# Template tier (mechanical) - always.
for artifact in (LENS_GLM, LENS_KIMI, LENS_CODEX, LENS_OPUS, SYNTH):
    name = os.path.basename(artifact)
    _check(f"artifact-exists-{name}", f"{name} exists", lambda a=artifact: os.path.isfile(a))
    _check(f"artifact-non-empty-{name}", f"{name} non-empty",
           lambda a=artifact: os.path.isfile(a) and os.path.getsize(a) > 0)

    def _parses(a=artifact):
        with open(a, "r", encoding="utf-8") as f:
            json.load(f)
        return True
    _check(f"artifact-json-parses-{name}", f"{name} parses as JSON", _parses)

# Task-specific tier.
_check("panel-four-lens-shape", "all lens artifacts expose assigned scores and null unassigned axes", _lens_shape)
_check("panel-synth-references-all-lenses", "chapter-review.json panel references all four lens score groups", _synth_references_lenses)
_check("panel-disagreement-recomputes", "panel_disagreement_score matches normalized variance formula", _disagreement_recomputes)

passed = not failed_checks

print(json.dumps({
    "verifier": "panel-completeness",
    "pass": passed,
    "evidence_path": EVIDENCE,
    "checks_run": checks_run,
    "failed_checks": failed_checks,
}))

ev.close()
sys.exit(0)
