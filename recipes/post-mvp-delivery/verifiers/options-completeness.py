#!/usr/bin/env python3
# Python port of options-completeness.sh (bash-removal WS8). Same rc semantics,
# env vars, and JSON output.

import json
import os
import re
import sys

PLAN_PATH = os.environ.get("MINI_ORK_PLAN_PATH", "")
RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR", "")

if not RUN_DIR and PLAN_PATH:
    RUN_DIR = os.path.dirname(PLAN_PATH)

OPTIONS_FILE = os.environ.get("ARTIFACT_PATH", "")
if not OPTIONS_FILE or OPTIONS_FILE == ".":
    OPTIONS_FILE = os.path.join(RUN_DIR or ".", "options.md")

missing = []
if not os.path.isfile(OPTIONS_FILE):
    missing.append("options.md")

if os.path.isfile(OPTIONS_FILE):
    text = open(OPTIONS_FILE, encoding="utf-8", errors="replace").read()
    checks = [
        (r"Option", re.I, "Option"),
        (r"Recommend(ed|ation)?", re.I, "Recommendation"),
        (r"Tradeoff", re.I, "Tradeoff"),
        (r"Risk", re.I, "Risk"),
        (r"Validation", re.I, "Validation"),
        (r"Decision", re.I, "Decision"),
    ]
    for pattern, flags, label in checks:
        if not re.search(pattern, text, flags):
            missing.append(label)

print(json.dumps({
    "verifier": "options-completeness",
    "pass": not missing,
    "options_file": OPTIONS_FILE,
    "missing": missing,
}))

sys.exit(0 if not missing else 1)
