#!/usr/bin/env python3
# verifiers/self-tests-pass.py — run mini-ork's own pytest suite inside
# the worktree the implementer patched. If any test fails, the patch
# is rejected and the runner routes to rollback.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR                run directory
#   MINI_ORK_SELF_IMPROVE_WORKTREE  worktree path (set by outer runner)
#   MINI_ORK_SELF_IMPROVE_TEST_CMD  override test command (shlex-split);
#                                   default: `python3 -m pytest -q`
#
# Output: JSON. Exit 0 always (caller reads .pass).
#
# History: this gate used to shell out to a fixed glob of `.sh` suites
# (tests/unit/test_*.sh, tests/integration/*.sh). The bash test tree was
# removed 2026-07 (Python is the only runtime), so those globs matched
# ZERO files and the gate hit "refusing vacuous pass" on every iter —
# a behavioral no-op that let a weak implementer's patch through the
# whole verifier chain untested (self-improve iter 2026-08-04 confirmed:
# tests=0, "no test suites found"). It now runs the real pytest suite —
# the same green gate `make test` / `make worktree-merge` enforce — so a
# patch that reds any test is actually caught here.

import json
import os
import re
import shlex
import subprocess

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
WT = os.environ.get("MINI_ORK_SELF_IMPROVE_WORKTREE") or os.environ.get("MINI_ORK_ROOT", "")
EVIDENCE = os.path.join(RUN_DIR, "verifier-self-tests-pass.log")
ev = open(EVIDENCE, "w")

try:
    os.chdir(WT)
except OSError:
    ev.write(f"worktree missing: {WT}\n")

# Run in DRY_RUN mode — the recipe integration tests honor this flag and
# skip live LLM dispatch, so the gate never fires paid provider calls.
os.environ["MINI_ORK_DRY_RUN"] = "1"

# The real test command. Default is the whole pytest suite — the same
# gate the human worktree-merge flow enforces — because "tighten the
# review" means the behavioral gate must actually exercise the code the
# patch touched, not a hand-picked subset a weak patch could sidestep.
# Operator can scope it (e.g. a fast layer) via MINI_ORK_SELF_IMPROVE_TEST_CMD.
DEFAULT_CMD = "python3 -m pytest -q"
cmd = shlex.split(os.environ.get("MINI_ORK_SELF_IMPROVE_TEST_CMD", "").strip() or DEFAULT_CMD)

ev.write(f"test_cmd={' '.join(cmd)}\ncwd={os.getcwd()}\n===== output =====\n")
ev.flush()

# Capture output so we can both stream it to the evidence log AND parse
# the pytest summary line for a collected-count (the vacuous guard).
proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
out = proc.stdout.decode("utf-8", "replace")
ev.write(out)
rc = proc.returncode

# pytest exit codes: 0=all passed, 1=tests failed, 2=interrupted,
# 3=internal error, 4=usage error, 5=NO TESTS COLLECTED. Parse the
# summary counts for evidence; the rc is authoritative for pass/fail.
def _count(word):
    m = re.search(rf"(\d+) {word}", out)
    return int(m.group(1)) if m else 0

n_passed = _count("passed")
n_failed = _count("failed") + _count("error") + _count("errors")
collected = n_passed + n_failed

# Vacuous-pass discipline: rc==5 (nothing collected) or a zero-test run
# is NOT a pass — that is exactly the failure mode this rewrite fixes.
if rc == 5 or (rc == 0 and collected == 0):
    ev.write("no tests collected — refusing vacuous pass\n")
    passed = 0
elif rc == 0:
    passed = 1
else:
    # rc==1 (failures) or any other non-zero (interrupt / internal /
    # usage error) → reject. A gate that can't run its own suite fails
    # closed, never open.
    passed = 0

ev.close()
print(json.dumps({
    "verifier": "self-tests-pass",
    "pass": passed == 1,
    "evidence_path": EVIDENCE,
    "test_cmd": " ".join(cmd),
    "pytest_rc": rc,
    "tests_collected": collected,
    "tests_passed": n_passed,
    "tests_failed": n_failed,
}))
