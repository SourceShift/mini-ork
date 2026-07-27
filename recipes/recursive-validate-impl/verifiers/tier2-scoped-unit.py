#!/usr/bin/env python3
# Runs tests under adjacent __tests__ directories for touched files. Avoids
# whole-repo test runs that exceed iteration budget.
#
# Python port of tier2-scoped-unit.sh (bash-removal WS8). Same rc semantics,
# evidence text, and JSON output (jq queries reimplemented with the json
# module).

import glob
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
_touched = summary.get("touched_files") or []
TOUCHED = "\n".join(_touched) if isinstance(_touched, list) else ""
READY = summary.get("ready_for_tier1", False)

EVIDENCE = os.path.join(RUN_DIR, "tier2-evidence.log")
ev = open(EVIDENCE, "w")

if READY is not True or not TOUCHED.strip():
    ev.write("implementer-summary is not ready for scoped unit tests or touched_files is empty\n")
    ev.close()
    print(json.dumps({
        "verifier": "tier2-scoped-unit",
        "pass": False,
        "evidence_path": EVIDENCE,
        "reason": "no implementer artifact ready_for_tier1=true with non-empty touched_files",
        "jest_rc": 1,
        "test_globs": "",
    }))
    sys.exit(1)

TEST_FILES = []


def add_test_file(candidate):
    if not os.path.isfile(candidate):
        return
    if re.search(r"\.(test|spec)\.(ts|tsx)$", candidate):
        TEST_FILES.append(candidate)


for f in TOUCHED.split():
    dir = os.path.dirname(f)
    base = os.path.basename(f)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    add_test_file(f)
    if os.path.isdir(os.path.join(dir, "__tests__")):
        for candidate in (
            os.path.join(dir, "__tests__", f"{stem}.test.ts"),
            os.path.join(dir, "__tests__", f"{stem}.test.tsx"),
            os.path.join(dir, "__tests__", f"{stem}.spec.ts"),
            os.path.join(dir, "__tests__", f"{stem}.spec.tsx"),
        ):
            add_test_file(candidate)

# dedupe preserving order (awk '!seen[$0]++')
TEST_FILES = list(dict.fromkeys(TEST_FILES))

jest_rc = 0
if not TEST_FILES:
    if re.search(r"\.(ts|tsx)$", TOUCHED, re.M):
        ev.write("no scoped tests found for touched TypeScript files - failing closed\n")
        passed = False
        jest_rc = 1
    else:
        ev.write("no scoped Jest tests for non-TypeScript touched files - passing with verifier/local evidence only\n")
        passed = True
        jest_rc = 0
else:
    pkg = ""
    try:
        pkg = open("package.json", encoding="utf-8", errors="replace").read()
    except OSError:
        pass
    has_vitest = bool(glob.glob("vitest.config.*")) or bool(glob.glob("config/vitest.config.*")) \
        or '"vitest"' in pkg
    env = None
    if has_vitest:
        # vitest project (e.g. Orca): jest+babel can't parse its TS test files
        # ("0 tests, 1 suite failed"). Use vitest with the repo's config.
        vcfg = ""
        for c in ("config/vitest.config.ts", "vitest.config.ts", "vitest.config.mts", "vitest.config.js"):
            if os.path.isfile(c):
                vcfg = c
                break
        if vcfg:
            argv = ["npx", "vitest", "run", "--config", vcfg] + TEST_FILES
        else:
            argv = ["npx", "vitest", "run"] + TEST_FILES
    elif os.path.isfile("server/jest.config.js"):
        env = {**os.environ, "JEST_GUARD_SOFT": "1"}
        argv = ["pnpm", "test:server", "--runTestsByPath"] + TEST_FILES
    else:
        argv = ["npx", "jest", "--runTestsByPath"] + TEST_FILES
    jest_rc = subprocess.run(argv, env=env, stdout=ev, stderr=subprocess.STDOUT).returncode
    passed = jest_rc == 0

ev.close()
print(json.dumps({
    "verifier": "tier2-scoped-unit",
    "pass": passed,
    "evidence_path": EVIDENCE,
    "jest_rc": jest_rc,
    "test_files": " ".join(TEST_FILES),
}))
sys.exit(0 if passed else 1)
