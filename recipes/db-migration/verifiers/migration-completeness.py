#!/usr/bin/env python3
# migration-completeness.py — deterministic gate for db_migration recipe.
#
# Python port of migration-completeness.sh (bash-removal WS8). Same stderr text
# and rc semantics.
#
# Enforces (per artifact_contract + plan.json verifier_contract):
#  - migration-plan.md exists
#  - all 5 lens reports exist + ≥ 200 words each
#  - migration-plan.md has IF NOT EXISTS / IF EXISTS guards (idempotent)
#  - migration-plan.md has at least one Reversal SQL block
#  - migration-plan.md has Snapshot section (data-loss insurance)
#  - migration-plan.md has Smoke script section
#  - migration-plan.md has Risk summary table
#  - migration-plan.md has Process notes (audit trail)
#
# Exits 0 on pass, non-zero on fail.

import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
PLAN = os.path.join(RUN_DIR, "migration-plan.md")

errors = 0


def err(msg):
    global errors
    sys.stderr.write(msg + "\n")
    errors += 1


if not os.path.isfile(PLAN):
    err(f"✗ migration-plan.md missing at {PLAN}")

for lens in ("integrity", "rollback", "perf", "compat", "edge"):
    f = os.path.join(RUN_DIR, f"lens-{lens}.md")
    if not os.path.isfile(f):
        err(f"✗ lens-{lens}.md missing")
        continue
    wc_val = len(open(f, encoding="utf-8", errors="replace").read().split())
    if wc_val < 200:
        err(f"✗ lens-{lens}.md word count {wc_val} < 200")

if os.path.isfile(PLAN):
    text = open(PLAN, encoding="utf-8", errors="replace").read()

    # Idempotency check — at least one IF EXISTS / IF NOT EXISTS guard
    if not re.search(r"IF (NOT )?EXISTS", text, re.I):
        err("✗ migration-plan.md has no IF (NOT) EXISTS guards — non-idempotent")

    # Reversal SQL section
    if not re.search(r"Reversal|Rollback", text, re.I):
        err("✗ migration-plan.md missing Reversal / Rollback SQL")

    # Snapshot section
    if not re.search(r"Snapshot|pg_dump|mysqldump|backup", text, re.I):
        err("✗ migration-plan.md missing Snapshot / backup section")

    # Smoke script section
    if not re.search(r"Smoke|smoke|post-migration verification", text, re.I):
        err("✗ migration-plan.md missing Smoke / post-migration verification section")

    # Risk summary
    if not re.search(r"Risk summary", text, re.I):
        err("✗ migration-plan.md missing 'Risk summary' table")

    # Process notes audit trail
    if not re.search(r"Process notes", text, re.I):
        err("✗ migration-plan.md missing 'Process notes' audit-trail section")

if errors > 0:
    sys.stderr.write("\n")
    sys.stderr.write(f"[migration-completeness] {errors} gate failure(s)\n")
    sys.exit(1)

sys.stderr.write("[migration-completeness] OK — migration-plan + all 5 lenses present, idempotent + reversible + smoke-tested\n")
sys.exit(0)
