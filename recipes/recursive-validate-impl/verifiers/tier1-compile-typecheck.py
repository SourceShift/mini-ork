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
import shlex
import subprocess
import sys

# mini-ork's verifiers are standalone scripts. The executor exports PYTHONPATH
# (`mini_ork/cli/execute.py`, _run_verifier_ref), but walk up to the engine root
# anyway so a hand-run or a caller that forgets it still imports.
_engine = os.path.dirname(os.path.realpath(__file__))
while not os.path.isdir(os.path.join(_engine, "mini_ork")) and _engine != os.path.dirname(_engine):
    _engine = os.path.dirname(_engine)
if _engine not in sys.path:
    sys.path.insert(0, _engine)
from mini_ork.verify.pm import describe, exec_argv, run_argv  # noqa: E402

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
ev.write(describe(".") + "\n")
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
    # The manager is a property of the TARGET repo, never the engine host. A
    # hardcoded `pnpm` here fails against an npm repo (and `npx` below fails
    # against a pnpm repo) — a TOOLCHAIN failure that gets read as a CODE
    # failure, which is how one bad lint call cascaded into 10 failed nodes.
    tsc_argv = run_argv(".", "type-check:touched", *SOURCE_FILES)
    ev.write(f"$ {shlex.join(tsc_argv)}\n")
    ev.flush()
    tsc_rc = subprocess.run(tsc_argv, stdout=ev, stderr=subprocess.STDOUT).returncode
else:
    ev.write("no TypeScript/JavaScript source files for tier1 typecheck\n")
    ev.flush()

if SOURCE_FILES and any(os.path.isfile(f) for f in
                        (".eslintrc", ".eslintrc.json", "eslint.config.js", "eslint.config.mjs")):
    lint_cmd = os.environ.get("MO_TIER1_LINT_CMD", "").strip()
    if lint_cmd == "skip":
        ev.write("lint skipped via MO_TIER1_LINT_CMD=skip (consumer repo gates lint "
                 "elsewhere, e.g. pre-commit hooks; eslint may structurally exclude "
                 "the touched tree)\n")
    elif lint_cmd:
        lint_argv = shlex.split(lint_cmd) + SOURCE_FILES
        ev.write(f"$ {shlex.join(lint_argv)}\n")
        ev.flush()
        lint_rc = subprocess.run(lint_argv,
                                 stdout=ev, stderr=subprocess.STDOUT, timeout=600).returncode
        ev.write(f"lint via MO_TIER1_LINT_CMD rc={lint_rc}\n")
    else:
        # `npx eslint` against a repo declaring devEngines.packageManager=pnpm
        # makes npm refuse to start (EBADDEVENGINES) before eslint runs at all.
        # exec_argv prefers the repo's own node_modules/.bin/eslint, then its
        # declared manager — never the engine host's npm.
        lint_argv = exec_argv(".", "eslint", *SOURCE_FILES)
        ev.write(f"$ {shlex.join(lint_argv)}\n")
        ev.flush()
        lint_rc = subprocess.run(lint_argv,
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
