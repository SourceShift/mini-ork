#!/usr/bin/env python3
# harness-shape.py — verifier for harness-bridge recipe.
#
# Python port of harness-shape.sh (bash-removal WS8). Same evidence text, JSON
# schema, and rc semantics.
#
# Checks that the harness wrapper emitted the expected artifacts:
#   1. harness-verdict.json is well-formed JSON
#   2. it names a known harness
#   3. if a diff was produced, it parses as a unified diff
#
# Per mini-ork verifier contract: emit JSON on stdout, exit 0
# regardless of pass/fail (the JSON result.pass is the signal).

import json
import os
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "harness-verifier-evidence.log")
ev = open(EVIDENCE, "w")

VERDICT_FILE = os.path.join(RUN_DIR, "harness-verdict.json")
DIFF_FILE = os.path.join(RUN_DIR, "harness.diff")

artifact_verdict_exists = False
verdict_parses = False
harness_known = False
diff_shape_ok = True  # default true when no diff produced

if os.path.isfile(VERDICT_FILE):
    artifact_verdict_exists = True
    ev.write("[ok] harness-verdict.json exists\n")
    try:
        verdict = json.load(open(VERDICT_FILE, encoding="utf-8"))
        verdict_parses = True
    except Exception:
        verdict = None
    if verdict_parses:
        ev.write("[ok] harness-verdict.json parses as JSON\n")
        harness_name = verdict.get("harness", "") if isinstance(verdict, dict) else ""
        harness_known = harness_name in ("claude-code", "codex-cli", "gemini-cli")
        if harness_known:
            ev.write(f"[ok] harness recognized: {harness_name}\n")
        else:
            ev.write(f"[fail] unknown harness in verdict: {harness_name}\n")

if os.path.isfile(DIFF_FILE) and os.path.getsize(DIFF_FILE) > 0:
    if any(l.startswith("diff --git")
           for l in open(DIFF_FILE, encoding="utf-8", errors="replace")):
        diff_shape_ok = True
        ev.write("[ok] diff shape valid\n")
    else:
        diff_shape_ok = False
        ev.write("[fail] diff present but lacks 'diff --git' anchor\n")

ev.close()

passed = artifact_verdict_exists and verdict_parses and harness_known and diff_shape_ok

print(json.dumps({
    "verifier": "harness-shape",
    "pass": passed,
    "evidence_path": EVIDENCE,
    "artifact_verdict_exists": artifact_verdict_exists,
    "verdict_parses": verdict_parses,
    "harness_known": harness_known,
    "diff_shape_ok": diff_shape_ok,
}))

sys.exit(0)
