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
from mini_ork.verify.pm import exec_argv, run_argv  # noqa: E402


def _root_multi_project_config() -> str:
    """The repo's root jest config *iff* it declares ``projects``.

    A repo that keeps two jest projects (frontend specs on jsdom, server specs
    on node) ships a root config whose entire purpose is to route each test
    path to the project whose ``testMatch`` claims it. Sending every test file
    this node was handed to ``test:server`` instead makes jest answer
    "No tests found, exiting with code 1" for ``src/**`` specs — which the node
    then reads as a CODE failure (run-1791821930-72687, 2026-10-12: two
    frontend specs failed tier2 with "No files found in <repo root>" while
    `jest --config jest.config.js --listTests` claimed both).
    """
    for cfg in ("jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs"):
        if not os.path.isfile(cfg):
            continue
        try:
            with open(cfg, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if re.search(r"\bprojects\s*:", text):
            return cfg
    return ""


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
            argv = exec_argv(".", "vitest", "run", "--config", vcfg, *TEST_FILES)
        else:
            argv = exec_argv(".", "vitest", "run", *TEST_FILES)
    elif os.path.isfile("server/jest.config.js"):
        env = {**os.environ, "JEST_GUARD_SOFT": "1"}
        root_cfg = _root_multi_project_config()
        if root_cfg:
            # The repo's own router beats picking a side: it claims every test
            # file this node was handed, in one run. Keep the repo's load guard
            # when it ships one — the guard is the reason `test:server` exists
            # as a script rather than a bare jest call.
            extra = ["--config", root_cfg, "--runTestsByPath", *TEST_FILES]
            guard = "scripts/jest-guard.sh"
            argv = (["bash", guard, "raw", "--", *extra] if os.path.isfile(guard)
                    else exec_argv(".", "jest", *extra))
        else:
            argv = run_argv(".", "test:server", "--runTestsByPath", *TEST_FILES)
    else:
        argv = exec_argv(".", "jest", "--runTestsByPath", *TEST_FILES)
    ev.write(f"$ {shlex.join(argv)}\n")
    ev.flush()
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
