#!/usr/bin/env python3
# Validate that feature-index.json contains enough extracted features.
#
# Python port of extraction-completeness.sh (bash-removal WS8). Same checks,
# evidence text, JSON schema, and rc semantics.

import json
import os

RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR") or os.getcwd()
NAME = "extraction-completeness"
FEATURE_INDEX = os.path.join(RUN_DIR, "feature-index.json")
MIN_FEATURES = os.environ.get("MO_DOC_LOOP_MIN_FEATURES", "5")
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")

open(CHECKS_TSV, "w").close()
_ev = open(EVIDENCE, "w")
_tsv = open(CHECKS_TSV, "a")


def check(cid, desc, fn):
    _ev.write(f"[{cid}] {desc}\n")
    _ev.flush()
    try:
        ok = bool(fn())
    except Exception as exc:
        _ev.write(f"{type(exc).__name__}: {exc}\n")
        ok = False
    _tsv.write(f"{cid}\t{desc}\t{'true' if ok else 'false'}\n")
    _tsv.flush()


def _features():
    data = json.load(open(FEATURE_INDEX, encoding="utf-8"))
    return data.get("features", data if isinstance(data, list) else [])


check("feature-index-exists", "feature-index.json exists and is non-empty",
      lambda: os.path.isfile(FEATURE_INDEX) and os.path.getsize(FEATURE_INDEX) > 0)
def _json_valid():
    json.load(open(FEATURE_INDEX, encoding="utf-8"))
    return True


check("feature-index-json-valid", "feature-index.json parses as JSON", _json_valid)


def _feature_count_minimum():
    features = _features()
    assert isinstance(features, list), "features must be a list"
    minimum = int(MIN_FEATURES)
    assert len(features) >= minimum, f"expected >= {minimum} features, got {len(features)}"
    return True


check("feature-count-minimum", "feature count is at least MO_DOC_LOOP_MIN_FEATURES", _feature_count_minimum)


def _feature_required_fields():
    for i, feature in enumerate(_features()):
        assert feature.get("id"), f"feature {i} missing id"
        assert feature.get("title"), f"{feature.get('id', i)} missing title"
        assert feature.get("priority"), f"{feature.get('id', i)} missing priority"
        assert isinstance(feature.get("dependencies", []), list), f"{feature.get('id', i)} dependencies must be list"
    return True


check("feature-required-fields", "each feature has id, title, priority, and dependencies",
      _feature_required_fields)

checks = []
with open(CHECKS_TSV, encoding="utf-8") as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"id": cid, "description": desc, "pass": passed == "true"})
failed = [c["id"] for c in checks if not c["pass"]]
print(json.dumps({
    "verifier": NAME,
    "pass": not failed,
    "evidence_path": EVIDENCE,
    "checks_run": [c["id"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": failed,
    "artifact_ref": FEATURE_INDEX,
}))

_ev.close()
_tsv.close()
