#!/usr/bin/env python3
# verifiers/parity.py — the byte-parity moat for a fork migration.
#
# Python port of parity.sh (bash-removal WS8). Same rc semantics, env vars, and
# JSON output.
#
# Open forks compare both runtimes. Closed forks validate the durable passing
# pre-retirement receipt plus their standalone post-retirement contract.
# Reuses scripts/runtime-parity-harness.sh.
#
# Inputs (env): MINI_ORK_RUN_DIR (required), MINI_ORK_ROOT (repo root),
#               MO_FORK (the fork being migrated, e.g. "verify") — informational.
# Output: JSON to stdout with .pass. Exit code mirrors .pass.

import json
import os
import subprocess
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
REPO_ROOT = os.environ.get("MO_TARGET_CWD") or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
FORK = os.environ.get("MO_FORK", "")
HARNESS = os.path.join(REPO_ROOT, "scripts", "runtime-parity-harness.sh")
EVIDENCE = os.path.join(RUN_DIR, "verifier-parity.log")
FORK_TEST = os.path.join(REPO_ROOT, "tests", "unit", f"test_mini_ork_{FORK}_py.py")

passed = True
reasons = []

if FORK and os.path.isfile(FORK_TEST) and os.path.isfile(HARNESS):
    env = {k: v for k, v in os.environ.items()
           if k not in ("MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_RUN_ID",
                        "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS")}
    env["MO_PRE_RETIREMENT_REPORT"] = os.path.join(RUN_DIR, "pre-retirement-parity.json")
    env["MO_PRE_RETIREMENT_EVIDENCE"] = os.path.join(RUN_DIR, "pre-retirement-parity-evidence.log")
    with open(EVIDENCE, "wb") as f:
        rc = subprocess.run(["bash", HARNESS, FORK], cwd=REPO_ROOT, env=env,
                            stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        passed = False
        reasons.append(f"post-retirement contract failed: {FORK_TEST} — see verifier-parity.log")
elif not os.path.isfile(HARNESS):
    passed = False
    reasons.append(f"no fork parity test and runtime-parity-harness.sh not found at {HARNESS}")
else:
    with open(EVIDENCE, "wb") as f:
        rc = subprocess.run(["bash", HARNESS], stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        passed = False
        reasons.append("cross-runtime parity harness reported a divergence — see verifier-parity.log")

print(json.dumps({
    "name": "parity",
    "fork": FORK,
    "pass": passed,
    "evidence": EVIDENCE,
    "reasons": reasons,
}))

sys.exit(0 if passed else 1)
