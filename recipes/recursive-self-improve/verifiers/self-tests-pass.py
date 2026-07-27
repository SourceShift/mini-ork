#!/usr/bin/env python3
# verifiers/self-tests-pass.py — run mini-ork's own test suite inside
# the worktree the implementer patched. If any test fails, the patch
# is rejected and the runner routes to rollback.
#
# Python port of self-tests-pass.sh (bash-removal WS8). Same rc semantics,
# evidence text, and JSON output.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR        run directory
#   MINI_ORK_SELF_IMPROVE_WORKTREE  worktree path (set by outer runner)
#
# Output: JSON. Exit 0 always (caller reads .pass).

import glob
import json
import os
import stat
import subprocess

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
WT = os.environ.get("MINI_ORK_SELF_IMPROVE_WORKTREE") or os.environ.get("MINI_ORK_ROOT", "")
EVIDENCE = os.path.join(RUN_DIR, "verifier-self-tests-pass.log")
ev = open(EVIDENCE, "w")

try:
    os.chdir(WT)
except OSError:
    ev.write(f"worktree missing: {WT}\n")

# Run mini-ork's own tests in DRY_RUN mode — most recipe integration
# tests honor this flag and skip live LLM dispatch. Without it iter 1
# burned ~20 min walking the full live-mode suite against an
# un-patched worktree.
os.environ["MINI_ORK_DRY_RUN"] = "1"

# Coverage scope. Default: unit tests + the recipes' own integration
# smoke tests (the ones the self-improve loop is most likely to break).
# Operator override: MINI_ORK_SELF_IMPROVE_TEST_GLOBS as a space-
# separated glob list relative to the worktree root.
#
# Wider coverage (full integration sweep) is intentionally OFF here
# because the same gauntlet runs in CI on the merged branch — running
# it per-iter costs ~20 min of wall-clock without catching anything
# the per-patch unit + smoke pair misses.
DEFAULT_GLOBS = [
    "tests/unit/test_*.sh",
    "tests/integration/test_recursive_self_improve_recipe.sh",
    "tests/integration/test_post_mvp_delivery_recipe.sh",
    "tests/integration/test_bin_execute.sh",
    "tests/integration/test_d008_workflow_node_dag.sh",
]
GLOBS = os.environ.get("MINI_ORK_SELF_IMPROVE_TEST_GLOBS", "").split() or DEFAULT_GLOBS

# We deliberately reject vacuous-pass cases (e.g. zero tests).
ran = 0
failed = 0
suites = []


def _run_suite(name, argv):
    global ran, failed
    ev.write(f"===== {name} =====\n")
    ev.flush()
    if subprocess.run(argv, stdout=ev, stderr=subprocess.STDOUT).returncode == 0:
        suites.append(f"{name}:PASS")
    else:
        failed += 1
        suites.append(f"{name}:FAIL")
    ran += 1


for g in GLOBS:
    for t in sorted(glob.glob(os.path.join(WT, g))):
        if not os.path.isfile(t):
            continue
        if not os.access(t, os.X_OK):
            try:
                os.chmod(t, os.stat(t).st_mode | stat.S_IXUSR)
            except OSError:
                pass
        label = f"{os.path.basename(os.path.dirname(t))}:{os.path.basename(t)}"
        _run_suite(label, ["bash", t])

if ran == 0:
    ev.write("no test suites found — refusing vacuous pass\n")
    passed = 0
else:
    passed = 1
    if failed > 0:
        passed = 0

ev.close()
print(json.dumps({
    "verifier": "self-tests-pass",
    "pass": passed == 1,
    "evidence_path": EVIDENCE,
    "suites_run": ran,
    "suites_failed": failed,
    "suites": suites,
}))
