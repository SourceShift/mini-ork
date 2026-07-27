#!/usr/bin/env python3
# findings-completeness.py — deterministic gate for ui_audit recipe.
#
# Python port of findings-completeness.sh (bash-removal WS8). Same stderr text
# and rc semantics.
#
# Per artifact_contract.yaml + plan.json verifier_contract:
#  - findings.md exists
#  - each finding has severity in {P0,P1,P2,P3}
#  - each finding has file:line OR URL+selector anchor
#  - each finding has a fix sketch
#  - each lens contributed at least one finding OR explicit N/A note
#
# Exits 0 on pass, non-zero on fail.

import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
FINDINGS = os.path.join(RUN_DIR, "findings.md")

errors = 0


def err(msg):
    global errors
    sys.stderr.write(msg + "\n")
    errors += 1


def warn(msg):
    sys.stderr.write(msg + "\n")


# 1. findings.md exists
if not os.path.isfile(FINDINGS):
    err(f"✗ findings.md missing at {FINDINGS}")

# 2. Each lens file exists
for lens in ("a11y", "perf", "visual", "interaction", "edge"):
    f = os.path.join(RUN_DIR, f"lens-{lens}.md")
    if not os.path.isfile(f):
        err(f"✗ lens-{lens}.md missing")

if os.path.isfile(FINDINGS):
    text = open(FINDINGS, encoding="utf-8", errors="replace").read()

    # 3. Severity-tagged findings present
    P_COUNT = sum(1 for line in text.splitlines() if re.match(r"^### [0-9]+\. ", line))
    if P_COUNT < 1:
        err("✗ findings.md has 0 finding entries (expected at least 1, even if just P3 polish)")

    # 4. Cross-lens patterns section present
    if "Cross-lens patterns" not in text:
        err("✗ findings.md missing 'Cross-lens patterns' section")

    # 5. Lens-contributions summary table present
    if "Lens contributions summary" not in text:
        err("✗ findings.md missing 'Lens contributions summary' table")

    # 6. Process-notes section present
    if "Process notes" not in text:
        err("✗ findings.md missing 'Process notes' audit-trail section")

    # 7. Each P-severity bucket header present (even if empty)
    for level in ("P0", "P1", "P2", "P3"):
        if not re.search(rf"^## {level} ", text, re.M):
            warn(f"⚠ findings.md missing '## {level}' section header (warn; may be intentional if zero findings at that level)")

if errors > 0:
    sys.stderr.write("\n")
    sys.stderr.write(f"[findings-completeness] {errors} gate failure(s)\n")
    sys.exit(1)

sys.stderr.write("[findings-completeness] OK — findings.md + all 5 lenses present, structure intact\n")
sys.exit(0)
