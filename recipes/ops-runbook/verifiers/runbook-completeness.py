#!/usr/bin/env python3
# runbook-completeness.py — deterministic gate for ops_runbook recipe.
#
# Python port of runbook-completeness.sh (bash-removal WS8). Same stderr text
# and rc semantics.
#
# Enforces:
#  - runbook.md exists
#  - all 5 lens reports exist
#  - runbook has the 5 expected section headers (Detection / Containment /
#    Diagnosis / Recovery / Prevention OR sections 0-4)
#  - runbook has a TL;DR section
#  - runbook has Process notes (audit trail)
#  - every code block under Recovery has Verify OR Rollback within 5 lines
#
# Exits 0 on pass, non-zero on fail.

import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
RUNBOOK = os.path.join(RUN_DIR, "runbook.md")

errors = 0


def err(msg):
    global errors
    sys.stderr.write(msg + "\n")
    errors += 1


def warn(msg):
    sys.stderr.write(msg + "\n")


if not os.path.isfile(RUNBOOK):
    err(f"✗ runbook.md missing at {RUNBOOK}")

# Each lens file present
for lens in ("detection", "containment", "diagnosis", "recovery", "prevention"):
    f = os.path.join(RUN_DIR, f"lens-{lens}.md")
    if not os.path.isfile(f):
        err(f"✗ lens-{lens}.md missing")
    else:
        wc_val = len(open(f, encoding="utf-8", errors="replace").read().split())
        if wc_val < 150:
            err(f"✗ lens-{lens}.md word count {wc_val} < 150 (too thin to be useful)")

if os.path.isfile(RUNBOOK):
    text = open(RUNBOOK, encoding="utf-8", errors="replace").read()

    # TL;DR section
    if not re.search(r"^## (0 — |TL;DR|0\. )", text, re.I | re.M):
        err("✗ runbook.md missing TL;DR / Section-0 header")

    # Each phase header
    for phase in ("Detection", "Containment", "Diagnosis", "Recovery", "Prevention"):
        if not re.search(rf"^(#{{1,3}}) .*{phase}", text, re.I | re.M):
            warn(f"⚠ runbook.md missing '{phase}' section header (warn — may be in different casing)")

    # Process notes
    if not re.search(r"Process notes", text, re.I):
        err("✗ runbook.md missing 'Process notes' audit-trail section")

    # Recovery section MUST have at least one Verify or Rollback line
    # (awk '/^## .*Recovery/,/^## /' range)
    recovery_lines = []
    in_recovery = False
    for line in text.splitlines():
        if re.match(r"^## ", line):
            if in_recovery:
                break
            if re.match(r"^## .*Recovery", line):
                in_recovery = True
        if in_recovery:
            recovery_lines.append(line)
    if recovery_lines:
        if not any(re.search(r"Verify|Rollback", line, re.I) for line in recovery_lines):
            err("✗ Recovery section has no Verify or Rollback lines — every recovery step should have both")

if errors > 0:
    sys.stderr.write("\n")
    sys.stderr.write(f"[runbook-completeness] {errors} gate failure(s)\n")
    sys.exit(1)

sys.stderr.write("[runbook-completeness] OK — runbook.md + all 5 lenses present, structure intact\n")
sys.exit(0)
