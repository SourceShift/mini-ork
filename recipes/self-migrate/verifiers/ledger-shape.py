#!/usr/bin/env python3
# verifiers/ledger-shape.py — enforce the static-feature ledger as a deliverable.
#
# Python port of ledger-shape.sh (bash-removal WS8). Identical logic (the .sh
# was already a thin wrapper around an embedded Python program).
#
# The ledger is the migration's strategic payload (the cost/verifiability map),
# so a run does not pass unless the ledger exists, is well-formed, classifies
# every feature it lists, and fills the cost-down `opportunity` on every agentic
# row. It also cross-checks that functions changed in self-migrate.diff have a
# ledger row (best-effort — warns if the diff is absent, e.g. smoke shape).
#
# Inputs (env): MINI_ORK_RUN_DIR (required).
# Output: JSON to stdout with .pass. Exit code mirrors .pass.

import json
import os
import re
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
LEDGER = os.path.join(RUN_DIR, "static-feature-ledger.json")
DIFF = os.path.join(RUN_DIR, "self-migrate.diff")

reasons = []
ok = True

if not os.path.isfile(LEDGER):
    print(json.dumps({"name": "ledger-shape", "pass": False,
                      "reasons": [f"ledger missing at {LEDGER}"]}))
    sys.exit(1)

try:
    led = json.load(open(LEDGER, encoding="utf-8"))
except Exception as e:
    print(json.dumps({"name": "ledger-shape", "pass": False,
                      "reasons": [f"ledger not valid JSON: {e}"]}))
    sys.exit(1)

feats = led.get("features")
if not isinstance(feats, list) or not feats:
    ok = False
    reasons.append("ledger.features must be a non-empty list")
    feats = feats or []

VALID_CLASS = {"static", "agentic", "integration"}
named = set()
symbols = set()
for i, f in enumerate(feats):
    name = f.get("feature")
    named.add(name)
    if isinstance(name, str):
        symbols.add(re.split(r"[.:]", name)[-1])
    if not name:
        ok = False
        reasons.append(f"feature[{i}] missing name")
    if f.get("class") not in VALID_CLASS:
        ok = False
        reasons.append(f"{name}: class must be one of {sorted(VALID_CLASS)}")
    # every agentic row must carry a cost-down opportunity (or an explicit null-reason)
    if f.get("class") == "agentic" and not f.get("opportunity"):
        ok = False
        reasons.append(f"{name}: agentic row must fill 'opportunity' (cost-down analysis)")

# cross-check: functions changed in the diff should appear in the ledger
if os.path.isfile(DIFF):
    added = open(DIFF, encoding="utf-8", errors="replace").read()
    changed_fns = set(re.findall(r"^\+\s*def\s+([a-zA-Z_]\w*)", added, re.M))
    missing = [fn for fn in changed_fns if fn not in symbols and not fn.startswith("_")]
    if missing:
        ok = False
        reasons.append("changed functions with no ledger row: " + ", ".join(sorted(missing)))
else:
    reasons.append("note: self-migrate.diff absent (smoke shape) — diff↔ledger cross-check skipped")

print(json.dumps({"name": "ledger-shape", "pass": ok,
                  "features": len(feats), "reasons": reasons}))
sys.exit(0 if ok else 1)
