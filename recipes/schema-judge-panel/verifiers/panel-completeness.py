#!/usr/bin/env python3
# Python port of panel-completeness.sh (bash-removal WS8). Same evidence text,
# JSON schema, and rc semantics.

import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-panel-completeness.log")
ev = open(EVIDENCE, "w")

missing = []
reports = [
    "judge-opus-scalability.md",
    "judge-opus-llm-safety.md",
    "judge-kimi-correctness.md",
    "judge-codex-codebase.md",
    "judge-minimax-performance.md",
]

EVIDENCE_RE = re.compile(r"Discovery Evidence|information_schema|pg_stat|rg |server/|docs/|SELECT|file:line|:[0-9]+", re.I)
PLAN_RE = re.compile(r"^#+ +Migration Plan|phased|phase|gate|rollback|backfill", re.I)

for report in reports:
    f = os.path.join(RUN_DIR, report)
    if not os.path.isfile(f):
        ev.write(f"MISSING: {report}\n")
        missing.append(report)
        continue
    text = open(f, encoding="utf-8", errors="replace").read()
    lines = text.count("\n")
    evidence_hits = sum(1 for line in text.splitlines() if EVIDENCE_RE.search(line))
    plan_hits = sum(1 for line in text.splitlines() if PLAN_RE.search(line))
    ev.write(f"{report}: lines={lines} evidence_hits={evidence_hits} plan_hits={plan_hits}\n")
    if lines < 20:
        missing.append(f"{report} too_short")
    if evidence_hits < 2:
        missing.append(f"{report} missing_discovery_evidence")
    if plan_hits < 1:
        missing.append(f"{report} missing_plan")

synth = os.path.join(RUN_DIR, "synthesis.md")
if not os.path.isfile(synth):
    missing.append("synthesis.md")
else:
    synth_text = open(synth, encoding="utf-8", errors="replace").read()
    for needle in ("opus-scalability", "opus-llm", "kimi", "codex", "minimax"):
        if not re.search(re.escape(needle), synth_text, re.I):
            missing.append(f"synthesis.md missing_{needle}")

ev.close()

print(json.dumps({
    "verifier": "panel-completeness",
    "pass": not missing,
    "evidence_path": EVIDENCE,
    "missing": missing,
}))

sys.exit(0)
