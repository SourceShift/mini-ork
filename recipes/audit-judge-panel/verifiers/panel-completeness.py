#!/usr/bin/env python3
# Completeness gate for the audit-judge-panel recipe: both judge reports must
# exist, carry a verdict block for every finding F1..F6 with a valid verdict
# enum and at least one evidence receipt, and the synthesis must cover both
# judges and all six findings.

import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.path.join(RUN_DIR, "verifier-panel-completeness.log")
ev = open(EVIDENCE, "w")

missing = []
reports = ["judge-opus-audit.md", "judge-minimax-audit.md"]
VALID_VERDICTS = {"confirmed", "partially_confirmed", "refuted", "unverifiable"}
VALID_SEVERITY = {"critical", "high", "medium", "low"}

# Receipts: SQL fragments, file paths with :line, sqlite3 invocations.
EVIDENCE_RE = re.compile(
    r"sqlite3|SELECT |FROM llm_calls|FROM execution_traces|FROM gradient_records"
    r"|writeback\.py|llm_dispatch\.py|:[0-9]+",
    re.I,
)
VERDICT_RE = re.compile(r"[\*`]*verdict[\*`]*\s*[:=][\s\*`]*(\w+)", re.I)
SEVERITY_RE = re.compile(r"[\*`]*severity[\*`]*\s*[:=][\s\*`]*(\w+)", re.I)

for report in reports:
    f = os.path.join(RUN_DIR, report)
    if not os.path.isfile(f):
        ev.write(f"MISSING: {report}\n")
        missing.append(report)
        continue
    text = open(f, encoding="utf-8", errors="replace").read()
    lines = text.count("\n")
    evidence_hits = sum(1 for line in text.splitlines() if EVIDENCE_RE.search(line))

    verdicts = VERDICT_RE.findall(text)
    severities = SEVERITY_RE.findall(text)
    invalid_verdicts = [v for v in verdicts if v.lower() not in VALID_VERDICTS]
    invalid_severity = [s for s in severities if s.lower() not in VALID_SEVERITY]

    ev.write(
        f"{report}: lines={lines} evidence_hits={evidence_hits} "
        f"verdict_blocks={len(verdicts)} severity_blocks={len(severities)}\n"
    )
    if lines < 30:
        missing.append(f"{report} too_short")
    if evidence_hits < 3:
        missing.append(f"{report} missing_discovery_evidence")
    if len(verdicts) < 6:
        missing.append(f"{report} incomplete_verdict_coverage({len(verdicts)}/6)")
    if invalid_verdicts:
        missing.append(f"{report} invalid_verdict_enum:{invalid_verdicts[:3]}")
    if invalid_severity:
        missing.append(f"{report} invalid_severity_enum:{invalid_severity[:3]}")

synth = os.path.join(RUN_DIR, "synthesis.md")
if not os.path.isfile(synth):
    missing.append("synthesis.md")
else:
    synth_text = open(synth, encoding="utf-8", errors="replace").read()
    for needle in ("opus", "minimax", "F1", "F2", "F3", "F4", "F5", "F6"):
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
