#!/usr/bin/env python3
# verifiers/cohesion-completeness.py — verify all 5 lens JSONs +
# applied_post.md + apply_log.md exist, parse, and meet structural floors.
#
# Python port of cohesion-completeness.sh (bash-removal WS8). Same evidence
# text, JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "cohesion-completeness", "pass": bool,
#     "evidence_path": "...", "lenses_passed": N, "missing": [...] }
#
# Exit codes: always 0 (caller reads .pass from JSON).

import json
import os
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]

EVIDENCE = os.path.join(RUN_DIR, "verifier-cohesion-completeness.log")
ev = open(EVIDENCE, "w")

missing = []
lenses_passed = 0

for lens in ("thesis", "bridge", "topic", "entity", "rhythm"):
    f = os.path.join(RUN_DIR, f"context-{lens}.json")
    if not os.path.isfile(f):
        ev.write(f"MISSING: {f}\n")
        missing.append(f"context-{lens}.json")
        continue
    # Parseable JSON with top-level verdict field
    try:
        verdict = json.load(open(f, encoding="utf-8")).get("verdict", "NO_VERDICT")
    except Exception as e:
        verdict = f"UNPARSEABLE: {e}"
    ev.write(f"context-{lens}.json: verdict={verdict}\n")
    if verdict in ("PASS", "REQUEST_CHANGES"):
        lenses_passed += 1
    else:
        missing.append(f"context-{lens}.json (verdict={verdict})")

# applied_post.md present + non-empty + frontmatter preserved
applied = os.path.join(RUN_DIR, "applied_post.md")
if not os.path.isfile(applied):
    missing.append("applied_post.md")
    ev.write(f"MISSING: {applied}\n")
else:
    ap_size = os.path.getsize(applied)
    ap_text = open(applied, encoding="utf-8", errors="replace").read()
    ap_words = len(ap_text.split())
    ev.write(f"applied_post.md: {ap_size} bytes, {ap_words} words\n")
    if ap_size < 200:
        missing.append(f"applied_post.md (too small: {ap_size} bytes)")
    # Frontmatter check — must start with `---` and contain `title:` + `pubDate:`
    lines = ap_text.splitlines()
    if not lines or lines[0] != "---":
        missing.append("applied_post.md (missing frontmatter opener)")
    head30 = lines[:30]
    if not any(l.startswith("title:") for l in head30):
        missing.append("applied_post.md (missing title in frontmatter)")
    if not any(l.startswith("pubDate:") for l in head30):
        missing.append("applied_post.md (missing pubDate in frontmatter)")

# apply_log.md present + has required sections
log = os.path.join(RUN_DIR, "apply_log.md")
if not os.path.isfile(log):
    missing.append("apply_log.md")
    ev.write(f"MISSING: {log}\n")
else:
    log_text = open(log, encoding="utf-8", errors="replace").read()
    for section in ("Inputs read", "Applied", "Rejected", "Open carve-outs"):
        if not any(l.startswith(f"## {section}") for l in log_text.splitlines()):
            missing.append(f"apply_log.md (missing section: {section})")

ev.close()

print(json.dumps({
    "verifier": "cohesion-completeness",
    "pass": not missing,
    "evidence_path": EVIDENCE,
    "lenses_passed": lenses_passed,
    "missing": missing,
}))

sys.exit(0)
