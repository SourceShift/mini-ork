#!/usr/bin/env python3
# Python port of selected-option-gate.sh (bash-removal WS8). Same rc semantics,
# env vars, and JSON output.

import json
import os
import re
import sys

PLAN_PATH = os.environ.get("MINI_ORK_PLAN_PATH", "")
RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR", "")
PROFILE_PATH = os.environ.get("MINI_ORK_PROFILE_PATH", "")

if not RUN_DIR and PLAN_PATH:
    RUN_DIR = os.path.dirname(PLAN_PATH)

SELECTED_FILE = os.path.join(RUN_DIR or ".", "selected-option.md")
OPTIONS_FILE = os.path.join(RUN_DIR or ".", "options.md")

preselected = "false"
kickoff_path = ""
if PROFILE_PATH and os.path.isfile(PROFILE_PATH):
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            kickoff_path = (json.load(f).get("kickoff_path") or "").strip()
    except Exception:
        kickoff_path = ""

if kickoff_path and os.path.isfile(kickoff_path):
    if re.search(r"^(selected|preselected)[ _-]*option\s*:",
                 open(kickoff_path, encoding="utf-8", errors="replace").read(),
                 re.I | re.M):
        preselected = "true"

missing = []
if not os.path.isfile(OPTIONS_FILE):
    missing.append("options.md")

if not (os.path.isfile(SELECTED_FILE) and os.path.getsize(SELECTED_FILE) > 0) \
        and preselected != "true":
    missing.append("selected-option.md or kickoff Selected Option:")

print(json.dumps({
    "verifier": "selected-option-gate",
    "pass": not missing,
    "selected_file": SELECTED_FILE,
    "options_file": OPTIONS_FILE,
    "preselected_by_kickoff": preselected == "true",
    "missing": missing,
}))

sys.exit(0 if not missing else 1)
