#!/usr/bin/env python3
# Verify P0 features carry arxiv-search-tool modern techniques references.
#
# Python port of arxiv-compliance-check.sh (bash-removal WS8). Same checks,
# evidence text, JSON schema, and rc semantics.

import json
import os

RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR") or os.getcwd()
NAME = "arxiv-compliance-check"
FEATURE_INDEX = os.path.join(RUN_DIR, "feature-index.json")
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


check("feature-index-exists", "feature-index.json exists",
      lambda: os.path.isfile(FEATURE_INDEX) and os.path.getsize(FEATURE_INDEX) > 0)


def _p0_modern_techniques_present():
    features = _features()
    p0 = [f for f in features if f.get("priority") == "P0"]
    assert p0, "no P0 features found"
    missing = []
    for feature in p0:
        refs = feature.get("modern_techniques_refs")
        if not isinstance(refs, list) or not refs:
            missing.append(feature.get("id", "<missing-id>"))
    assert not missing, f"P0 features missing modern_techniques_refs: {missing}"
    return True


check("p0-modern-techniques-present", "each P0 feature has modern_techniques_refs",
      _p0_modern_techniques_present)


def _p0_arxiv_source_present():
    bad = []
    for feature in _features():
        if feature.get("priority") != "P0":
            continue
        refs = feature.get("modern_techniques_refs") or []
        text = json.dumps(refs).lower()
        if "arxiv-search-tool" not in text and "arxiv" not in text:
            bad.append(feature.get("id", "<missing-id>"))
    assert not bad, f"P0 features missing arxiv source evidence: {bad}"
    return True


check("p0-arxiv-search-tool-source-present", "each P0 reference names arxiv-search-tool or an arxiv source",
      _p0_arxiv_source_present)

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
