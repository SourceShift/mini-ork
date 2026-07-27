#!/usr/bin/env python3
# draft-completeness.py — deterministic gate for blog_post recipe.
#
# Python port of draft-completeness.sh (bash-removal WS8). Same stderr text and
# rc semantics.
#
# Per artifact_contract.yaml: draft.md exists + ≥ 0.8 × target_word_count +
# every lens-*.md exists at >= 200 words + synthesizer process-notes
# section present + no fabricated-looking citations.
#
# Exits 0 on pass, non-zero on fail. Errors to stderr.

import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
PLAN = os.path.join(RUN_DIR, "plan.json")
DRAFT = os.path.join(RUN_DIR, "draft.md")

errors = 0


def err(msg):
    global errors
    sys.stderr.write(msg + "\n")
    errors += 1


# 1. Plan exists
if not os.path.isfile(PLAN):
    err(f"✗ plan.json missing at {PLAN}")
    TARGET_WC = 1200
else:
    try:
        TARGET_WC = int(json.load(open(PLAN, encoding="utf-8")).get("target_word_count", 1200))
    except Exception:
        TARGET_WC = 1200

# 2. Draft exists and meets word-count floor
if not os.path.isfile(DRAFT):
    err(f"✗ draft.md missing at {DRAFT}")
else:
    draft_text = open(DRAFT, encoding="utf-8", errors="replace").read()
    DRAFT_WC = len(draft_text.split())
    FLOOR = int(0.8 * TARGET_WC)
    if DRAFT_WC < FLOOR:
        err(f"✗ draft.md word count {DRAFT_WC} < floor {FLOOR} (0.8 × target {TARGET_WC})")

# 3. Each lens-*.md exists at ≥ 200 words
for lens in ("editor", "researcher", "narrative", "audience", "counter"):
    f = os.path.join(RUN_DIR, f"lens-{lens}.md")
    if not os.path.isfile(f):
        err(f"✗ lens-{lens}.md missing")
        continue
    wc_val = len(open(f, encoding="utf-8", errors="replace").read().split())
    if wc_val < 200:
        err(f"✗ lens-{lens}.md word count {wc_val} < 200")

# 4. Synthesizer process-notes block present
if os.path.isfile(DRAFT) and "Process notes" not in open(DRAFT, encoding="utf-8", errors="replace").read():
    err("✗ draft.md missing 'Process notes' audit-trail section")

# 5. Citation-fabrication smell-check: every URL must be plausibly real.
# Heuristic — flag URLs with obviously-fake path components.
if os.path.isfile(DRAFT):
    urls = re.findall(r"https?://[^\s)]+", open(DRAFT, encoding="utf-8", errors="replace").read())
    bad = sum(1 for u in urls if re.search(r"/fake/|/example/|placeholder|/lorem/", u, re.I))
    if bad > 0:
        err(f"✗ draft.md contains {bad} URL(s) matching fabrication smell-test")

if errors > 0:
    sys.stderr.write("\n")
    sys.stderr.write(f"[draft-completeness] {errors} gate failure(s)\n")
    sys.exit(1)

sys.stderr.write("[draft-completeness] OK — draft + all 5 lenses + process-notes present\n")
sys.exit(0)
