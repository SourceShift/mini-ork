#!/usr/bin/env python3
# verifiers/lens-completeness.py — verify all 5 lens reports + anonymous synthesis
# exist, are non-empty, and cite at least one file:line anchor.
#
# Python port of lens-completeness.sh (bash-removal WS8). Same evidence text,
# JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "lens-completeness", "pass": bool, "evidence_path": "...",
#     "findings_count": N, "missing": [...] }
#
# Exit codes: always 0 (caller reads .pass from JSON).

import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]

EVIDENCE = os.path.join(RUN_DIR, "verifier-lens-completeness.log")
ev = open(EVIDENCE, "w")

LENSES = ("glm", "kimi", "codex", "opus", "minimax")

missing = []
findings_total = 0

for lens in LENSES:
    f = os.path.join(RUN_DIR, f"lens-{lens}.md")
    if not os.path.isfile(f):
        ev.write(f"MISSING: {f}\n")
        missing.append(f"lens-{lens}.md")
        continue
    # Non-empty + ≥1 file:line anchor (path:digit pattern)
    text = open(f, encoding="utf-8", errors="replace").read()
    lines = text.count("\n")
    anchors = sum(1 for line in text.splitlines() if re.search(r"[a-zA-Z_./-]+:[0-9]+", line))
    ev.write(f"lens-{lens}: {lines} lines, {anchors} anchors\n")
    if lines < 10:
        missing.append(f"lens-{lens}.md (too short: {lines} lines)")
    if anchors < 1:
        missing.append(f"lens-{lens}.md (no file:line anchors)")
    findings_total += lines

# The deterministic transform must materialize exactly one anonymous response
# per lens. It may preserve evidence text but must not expose source filenames
# through the consumer-facing response headings.
panel = os.path.join(RUN_DIR, "panel-responses.md")
if not os.path.isfile(panel):
    missing.append("panel-responses.md")
else:
    panel_text = open(panel, encoding="utf-8", errors="replace").read()
    n_responses = sum(1 for line in panel_text.splitlines() if re.match(r"^## Response [A-E]$", line))
    if n_responses != 5:
        missing.append("panel-responses.md (expected Response A through Response E)")

# Synthesis must exist + cross-reference all five anonymous responses. The
# synthesizer is deliberately not expected to reveal or recover lane identity.
synth = os.path.join(RUN_DIR, "synthesis.md")
if not os.path.isfile(synth):
    missing.append("synthesis.md")
else:
    synth_text = open(synth, encoding="utf-8", errors="replace").read()
    for response in "ABCDE":
        if not re.search(rf"Response {response}|{response}-[0-9]", synth_text):
            missing.append(f"synthesis.md (no reference to Response {response})")

ev.close()

print(json.dumps({
    "verifier": "lens-completeness",
    "pass": not missing,
    "evidence_path": EVIDENCE,
    "findings_count": findings_total,
    "missing": missing,
}))

sys.exit(0)
