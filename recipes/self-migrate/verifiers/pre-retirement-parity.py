#!/usr/bin/env python3
# verifiers/pre-retirement-parity.py — capture the Bash oracle before migration.
#
# Python port of pre-retirement-parity.sh (bash-removal WS8). Same rc semantics,
# env vars, and JSON output.
#
# This verifier is ordered before the migrator. Its evidence survives deletion
# of the legacy entrypoint in the proposed worktree diff, proving parity was
# established while both runtimes still existed.

import json
import os
import subprocess
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
REPO_ROOT = os.environ.get("MO_TARGET_CWD") or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
FORK = os.environ.get("MO_FORK", "")
FORK_TEST = os.path.join(REPO_ROOT, "tests", "unit", f"test_mini_ork_{FORK}_py.py")
HARNESS = os.path.join(REPO_ROOT, "scripts", "runtime-parity-harness.sh")
EVIDENCE = os.path.join(RUN_DIR, "pre-retirement-parity-evidence.log")
STATE = os.path.join(RUN_DIR, "pre-retirement-parity.json")

passed = True
reasons = []


def _state_is_passing():
    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return False
    return state.get("pass") is True


# A later recovery or verifier partition may revisit this node after the Bash
# entrypoint has been removed in the worktree. Reuse only a passing state from
# this unique run directory and only while its evidence file still exists.
if (os.path.isfile(STATE) and os.path.getsize(STATE) > 0
        and os.path.isfile(EVIDENCE) and os.path.getsize(EVIDENCE) > 0
        and _state_is_passing()):
    with open(STATE, encoding="utf-8") as f:
        sys.stdout.write(f.read())
    sys.exit(0)

if FORK and os.path.isfile(FORK_TEST):
    env = {k: v for k, v in os.environ.items()
           if k not in ("MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_RUN_ID",
                        "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS")}
    with open(EVIDENCE, "wb") as f:
        rc = subprocess.run([sys.executable, "-m", "pytest", FORK_TEST, "-q",
                             "-p", "no:cacheprovider"], cwd=REPO_ROOT, env=env,
                            stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        passed = False
        reasons.append(f"pre-retirement fork parity failed: {FORK_TEST}")
elif os.path.isfile(HARNESS):
    with open(EVIDENCE, "wb") as f:
        rc = subprocess.run(["bash", HARNESS], stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        passed = False
        reasons.append("pre-retirement runtime parity harness failed")
else:
    passed = False
    reasons.append("no pre-retirement parity oracle found")

result = {
    "name": "pre-retirement-parity",
    "fork": FORK,
    "pass": passed,
    "evidence": EVIDENCE,
    "reasons": reasons,
}
with open(STATE, "w", encoding="utf-8") as handle:
    json.dump(result, handle)
    handle.write("\n")
print(json.dumps(result))

sys.exit(0 if passed else 1)
