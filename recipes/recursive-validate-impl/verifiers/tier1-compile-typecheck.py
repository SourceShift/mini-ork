#!/usr/bin/env python3
# Fast deterministic gate. Runs in under 10s when project tooling supports it.
# Exits 0 with JSON to stdout per mini-ork verifier contract.
#
# Python port of tier1-compile-typecheck.sh (bash-removal WS8). Same rc
# semantics, evidence text, and JSON output (jq queries reimplemented with the
# json module).

import json
import os
import re
import subprocess
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]

try:
    summary = json.load(open(os.path.join(RUN_DIR, "implementer-summary.json"), encoding="utf-8"))
except Exception:
    summary = {}
TOUCHED_FILES = summary.get("touched_files") or []
if not isinstance(TOUCHED_FILES, list):
    TOUCHED_FILES = []
READY = summary.get("ready_for_tier1", False)

EVIDENCE = os.path.join(RUN_DIR, "tier1-evidence.log")
ev = open(EVIDENCE, "w")

if READY is not True or not TOUCHED_FILES:
    ev.write("implementer-summary is not ready for tier1 or touched_files is empty\n")
    ev.close()
    print(json.dumps({
        "verifier": "tier1-compile-typecheck",
        "pass": False,
        "evidence_path": EVIDENCE,
        "reason": "no implementer artifact ready_for_tier1=true with non-empty touched_files",
        "tsc_rc": 1,
        "lint_rc": 0,
    }))
    sys.exit(1)

SOURCE_FILES = []
SHELL_FILES = []
ARTIFACT_FILES = []
for path in TOUCHED_FILES:
    if path.startswith(".mini-ork/runs/") or "/.mini-ork/runs/" in path:
        ARTIFACT_FILES.append(path)
    elif re.search(r"\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$", path):
        SOURCE_FILES.append(path)
    elif path.endswith(".sh"):
        SHELL_FILES.append(path)

ev.write(f"touched_files={len(TOUCHED_FILES)}\n")
ev.write(f"source_files={len(SOURCE_FILES)}\n")
ev.write(f"shell_files={len(SHELL_FILES)}\n")
ev.write(f"artifact_files={len(ARTIFACT_FILES)}\n")
ev.flush()

tsc_rc = 0
lint_rc = 0
shell_rc = 0

pkg = ""
try:
    pkg = open("package.json", encoding="utf-8", errors="replace").read()
except OSError:
    pass

if SOURCE_FILES and os.path.isfile("package.json") and "type-check:touched" in pkg:
    tsc_rc = subprocess.run(["pnpm", "type-check:touched"] + SOURCE_FILES,
                            stdout=ev, stderr=subprocess.STDOUT).returncode
else:
    ev.write("no TypeScript/JavaScript source files for tier1 typecheck\n")
    ev.flush()

if SOURCE_FILES and any(os.path.isfile(f) for f in
                        (".eslintrc", ".eslintrc.json", "eslint.config.js", "eslint.config.mjs")):
    lint_rc = subprocess.run(["npx", "eslint"] + SOURCE_FILES,
                             stdout=ev, stderr=subprocess.STDOUT).returncode
else:
    ev.write("no source files for eslint or no eslint config found\n")
    ev.flush()

if SHELL_FILES:
    for path in SHELL_FILES:
        if subprocess.run(["bash", "-n", path],
                          stdout=ev, stderr=subprocess.STDOUT).returncode != 0:
            shell_rc = 1

passed = tsc_rc == 0 and lint_rc == 0 and shell_rc == 0

ev.close()
print(json.dumps({
    "verifier": "tier1-compile-typecheck",
    "pass": passed,
    "evidence_path": EVIDENCE,
    "tsc_rc": tsc_rc,
    "lint_rc": lint_rc,
    "shell_rc": shell_rc,
}))
sys.exit(0 if passed else 1)
