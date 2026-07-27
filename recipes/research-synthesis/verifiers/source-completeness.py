#!/usr/bin/env python3
# verifiers/source-completeness.py — verify all 4 lens reports + synthesis
# exist, are non-empty, and cite enough sources.
#
# Python port of source-completeness.sh (bash-removal WS8). Same evidence text,
# JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "source-completeness", "pass": bool, "evidence_path": "...",
#     "source_count": N, "missing": [...] }
#
# Exit codes: always 0 (caller reads .pass from JSON).

import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]

EVIDENCE = os.path.join(RUN_DIR, "verifier-source-completeness.log")
ev = open(EVIDENCE, "w")

LENSES = ("glm", "kimi", "codex", "opus")

missing = []
total_sources = 0

SOURCE_PATTERN = re.compile(r"https?://|arxiv:|github\.com/|\([A-Z][a-z]+ [0-9]{4}\)")

for lens in LENSES:
    f = os.path.join(RUN_DIR, f"lens-{lens}.md")
    if not os.path.isfile(f):
        ev.write(f"MISSING: {f}\n")
        missing.append(f"lens-{lens}.md")
        continue
    # Non-empty + minimum source count check
    text = open(f, encoding="utf-8", errors="replace").read()
    lines = text.count("\n")
    # Source-citation patterns:
    #  - http(s):// URLs
    #  - arxiv:XXXX.XXXXX
    #  - github.com/org/repo
    #  - (Author Year) parenthetical
    sources = sum(1 for line in text.splitlines() if SOURCE_PATTERN.search(line))
    ev.write(f"lens-{lens}: {lines} lines, {sources} sources\n")

    if lines < 20:
        missing.append(f"lens-{lens}.md (too short: {lines} lines)")
    # Per-lens minimum citation count
    min_required = 3 if lens == "opus" else 5  # opus is narrative; fewer but deeper citations OK
    if sources < min_required:
        missing.append(f"lens-{lens}.md (only {sources} sources, need ≥{min_required})")
    total_sources += sources

# Synthesis must exist + cross-reference all 4 lens names + use consensus markers
synth = os.path.join(RUN_DIR, "synthesis.md")
if not os.path.isfile(synth):
    missing.append("synthesis.md")
else:
    synth_text = open(synth, encoding="utf-8", errors="replace").read()
    for lens in LENSES:
        if not re.search(rf"(lens-)?{lens}", synth_text):
            missing.append(f"synthesis.md (no reference to {lens} lens)")
    # Consensus markers (★ unicode) — at least one should appear if there's any consensus.
    # Soft check; absence is a warning not a fail (legitimately disputed topics may have 0 consensus).
    consensus_count = sum(1 for line in synth_text.splitlines() if "★" in line)
    ev.write(f"synthesis.md: {consensus_count} consensus marker(s)\n")

ev.close()

print(json.dumps({
    "verifier": "source-completeness",
    "pass": not missing,
    "evidence_path": EVIDENCE,
    "source_count": total_sources,
    "missing": missing,
}))

sys.exit(0)
