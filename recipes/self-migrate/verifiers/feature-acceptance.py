#!/usr/bin/env python3
# verifiers/feature-acceptance.py — the end-to-end feature gate for a fork.
#
# Python port of feature-acceptance.sh (bash-removal WS8). Same probes, rc
# semantics, env vars, and JSON output.
#
# Unit-parity is necessary but not sufficient (a rewire can pass unit-parity yet
# break the feature — e.g. leak stdout). This runs (a) the fork's feature-
# acceptance probe from gates/feature_acceptance.sh and (b) the fork's Python
# test module + pyright, so a green here means the FEATURE works, not just a fn.
#
# Inputs (env): MINI_ORK_RUN_DIR (required), MINI_ORK_ROOT (repo root),
#               MO_FORK (the fork/feature, e.g. "verify").
# Output: JSON to stdout with .pass. Exit code mirrors .pass.

import json
import os
import shlex
import sys

# mini-ork's verifiers are standalone scripts. The executor exports PYTHONPATH
# (`mini_ork/cli/execute.py`, _run_verifier_ref), but walk up to the engine root
# anyway so a hand-run or a caller that forgets it still imports.
_engine = os.path.dirname(os.path.realpath(__file__))
while not os.path.isdir(os.path.join(_engine, "mini_ork")) and _engine != os.path.dirname(_engine):
    _engine = os.path.dirname(_engine)
if _engine not in sys.path:
    sys.path.insert(0, _engine)
from mini_ork.verify.pm import classify_suite_spawn, run_logged  # noqa: E402

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
REPO_ROOT = os.environ.get("MO_TARGET_CWD") or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
FORK = os.environ.get("MO_FORK", "")
GATE = os.path.join(REPO_ROOT, "gates", "feature_acceptance.sh")
EVIDENCE = os.path.join(RUN_DIR, "verifier-feature-acceptance.log")

open(EVIDENCE, "w").close()

passed = True
reasons = []
failed_probes = []  # "toolchain" or "code", one entry per failed probe


def _append_evidence(line):
    with open(EVIDENCE, "a") as f:
        f.write(line + "\n")


def _run_to_evidence(argv, cwd=None, env=None, shell=False):
    with open(EVIDENCE, "ab") as f:
        rc, tail = run_logged(argv, EVIDENCE, cwd=cwd, env=env, shell=shell, stream=f)
    if rc != 0:
        # rc 127 / a signal kill means the probe never ran: a missing binary,
        # not a failing feature. Each failed probe records WHICH it was, because
        # one probe's missing tool must not excuse another probe's real failure.
        kind = classify_suite_spawn(argv if isinstance(argv, list) else shlex.split(argv), rc, tail)
        failed_probes.append(kind or "code")
    return rc == 0


def _pytest(target):
    env = {k: v for k, v in os.environ.items()
           if k not in ("MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_RUN_ID",
                        "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS")}
    return _run_to_evidence([sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
                            cwd=REPO_ROOT, env=env)


# (a) the feature-acceptance probe for this fork's feature
if FORK and os.path.isfile(GATE) and os.access(GATE, os.X_OK):
    env = {**os.environ, "MO_FORK": FORK}
    if _run_to_evidence(["bash", GATE, FORK], env=env):
        _append_evidence(f"[feature-probe] {FORK} PASS")
    else:
        passed = False
        reasons.append(f"feature-acceptance probe for '{FORK}' failed")
elif not FORK:
    reasons.append("MO_FORK not set — cannot select a feature probe (shape-only)")

# (b) the fork's Python unit contracts
TESTF = os.path.join(REPO_ROOT, "tests", "unit", f"test_mini_ork_{FORK}_py.py")
if FORK and os.path.isfile(TESTF):
    if _pytest(TESTF):
        _append_evidence(f"[pytest] {TESTF} PASS")
    else:
        passed = False
        reasons.append(f"pytest {TESTF} failed")


def _integration(script):
    return _run_to_evidence(f"bash {script}", cwd=REPO_ROOT, shell=True)


# Reflect has additional inbound contracts beyond its focused unit module:
# GEPA's default path and the standalone CLI integration suite.
if FORK == "reflect":
    if _run_to_evidence([sys.executable, "-m", "pytest", "tests/test_gepa_wiring_py.py",
                         "-q", "-p", "no:cacheprovider"], cwd=REPO_ROOT):
        _append_evidence("[pytest] tests/test_gepa_wiring_py.py PASS")
    else:
        passed = False
        reasons.append("pytest tests/test_gepa_wiring_py.py failed")
    if _integration("tests/integration/test_bin_reflect.sh"):
        _append_evidence("[integration] tests/integration/test_bin_reflect.sh PASS")
    else:
        passed = False
        reasons.append("reflect integration suite failed")

# Classify has a broad inbound surface: shell integration callers plus the
# hostile-input contracts that protect its kickoff and environment boundary.
if FORK == "classify":
    if _integration("tests/integration/test_bin_classify.sh"):
        _append_evidence("[integration] tests/integration/test_bin_classify.sh PASS")
    else:
        passed = False
        reasons.append("classify integration suite failed")
    SECURITY_TESTS = [
        "tests/security/test_sec_env_var_pollution.sh",
        "tests/security/test_sec_hooks_attack_surface.sh",
        "tests/security/test_sec_kickoff_command_injection.sh",
        "tests/security/test_sec_kickoff_path_traversal.sh",
        "tests/security/test_sec_malformed_yaml.sh",
        "tests/security/test_sec_oversized_input.sh",
        "tests/security/test_sec_sql_injection_run_id.sh",
    ]
    for security_test in SECURITY_TESTS:
        if _integration(security_test):
            _append_evidence(f"[security] {security_test} PASS")
        else:
            passed = False
            reasons.append(f"{security_test} failed")

# CLI closure preserves an unsuffixed public launcher and its whole lifecycle
# contract, so include the dispatcher integration suite in addition to the
# focused standalone Python contract above.
if FORK == "cli":
    if _integration("tests/integration/test_bin_dispatcher.sh"):
        _append_evidence("[integration] tests/integration/test_bin_dispatcher.sh PASS")
    else:
        passed = False
        reasons.append("CLI dispatcher integration suite failed")

# Execute closure owns the public subcommand route plus the native outbound
# seams, so verify both its standalone golden contract and the real launcher.
if FORK == "execute":
    if _integration("tests/integration/test_bin_execute.sh"):
        _append_evidence("[integration] tests/integration/test_bin_execute.sh PASS")
    else:
        passed = False
        reasons.append("execute integration suite failed")

# Plan retirement has several executable callers whose contracts are broader
# than the focused unit module: module-level CLI behavior, given-plan bypass,
# recipe dry-runs, hostile kickoff input, and the web provenance surface.
if FORK == "plan":
    PLAN_TESTS = [
        "tests/integration/test_bin_plan.sh",
        "tests/integration/test_given_plan.sh",
        "tests/e2e/test_e2e_recipe_bdd_first.sh",
        "tests/e2e/test_e2e_recipe_code_fix.sh",
        "tests/security/test_sec_hooks_attack_surface.sh",
        "tests/security/test_sec_kickoff_command_injection.sh",
        "tests/security/test_sec_oversized_input.sh",
    ]
    for plan_test in PLAN_TESTS:
        if _integration(plan_test):
            _append_evidence(f"[plan-contract] {plan_test} PASS")
        else:
            passed = False
            reasons.append(f"{plan_test} failed")
    if _run_to_evidence([sys.executable, "-m", "pytest", "tests/test_web_smoke.py",
                         "-q", "-p", "no:cacheprovider"], cwd=REPO_ROOT):
        _append_evidence("[pytest] tests/test_web_smoke.py PASS")
    else:
        passed = False
        reasons.append("tests/test_web_smoke.py failed")

# (c) type-check the migrated port and the Python callers changed by the rewire.
TYPE_TARGETS = [f"mini_ork/cli/{FORK}.py"]
if FORK == "reflect":
    TYPE_TARGETS += ["mini_ork/cli/main.py", "mini_ork/cli/execute.py"]
if FORK == "classify":
    TYPE_TARGETS += ["mini_ork/cli/main.py", "mini_ork/web/routes/run_detail.py"]
if FORK == "plan":
    TYPE_TARGETS += ["mini_ork/cli/main.py"]
if FORK == "execute":
    TYPE_TARGETS += [
        "mini_ork/cli/main.py",
        "mini_ork/gates/intervention_gate.py",
        "mini_ork/dispatch/lane_helpers.py",
        "mini_ork/gates/gate_registry.py",
    ]
if FORK and os.path.isfile(os.path.join(REPO_ROOT, TYPE_TARGETS[0])):
    if _run_to_evidence([sys.executable, "-m", "pyright"] + TYPE_TARGETS, cwd=REPO_ROOT):
        _append_evidence(f"[pyright] {' '.join(TYPE_TARGETS)} 0 errors")
    else:
        passed = False
        reasons.append(f"pyright on {' '.join(TYPE_TARGETS)} not clean")

payload = {
    "name": "feature-acceptance",
    "fork": FORK,
    "pass": passed,
    "evidence": EVIDENCE,
    "reasons": reasons,
}
# A red verdict is only a refutation when a tool actually judged the code. If
# EVERY failed probe failed to launch, none of them judged anything and the
# honest verdict is an abstention. A single real failure keeps it a refutation —
# abstaining here would hide a regression behind a broken toolchain.
if not passed and failed_probes and all(k == "toolchain" for k in failed_probes):
    payload["failure_kind"] = "toolchain"
print(json.dumps(payload))

sys.exit(0 if passed else 1)
