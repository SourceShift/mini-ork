#!/usr/bin/env python3
# verifiers/recipe-validator.py — validate the authored framework-edit recipe tree.
#
# Python port of recipe-validator.sh (bash-removal WS8). Same checks, evidence
# text, JSON schema, and rc semantics.
#
# Optional arg: recipe directory to validate, used for self-tests.
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

import glob
import json
import os
import re
import subprocess
import sys

import yaml

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
REPO_ROOT = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
NAME = "recipe-validator"
if len(sys.argv) > 1 and sys.argv[1]:
    NAME = "recipe-validator-self-test"
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")

open(CHECKS_TSV, "w").close()
_ev = open(EVIDENCE, "w")
_tsv = open(CHECKS_TSV, "a")

if len(sys.argv) > 1 and sys.argv[1]:
    RECIPE_DIR = sys.argv[1]
else:
    try:
        RECIPE_NAME = re.sub(r"\s", "", open(os.path.join(RUN_DIR, "chosen", "recipe_name"),
                                             encoding="utf-8").read())
    except OSError:
        RECIPE_NAME = ""
    RECIPE_DIR = os.path.join(RUN_DIR, "chosen", RECIPE_NAME)


def _check(cid, desc, fn):
    _ev.write(f"[{cid}] {desc}\n")
    _ev.flush()
    try:
        ok = bool(fn())
    except Exception as exc:
        _ev.write(f"{type(exc).__name__}: {exc}\n")
        ok = False
    _tsv.write(f"{cid}\t{desc}\t{'true' if ok else 'false'}\n")
    _tsv.flush()
    _ev.write("  ok\n" if ok else "  FAIL\n")
    _ev.flush()


def _p(*parts):
    return os.path.join(RECIPE_DIR, *parts)


def _yaml_parses(path):
    yaml.safe_load(open(path, encoding="utf-8"))
    return True


def _workflow():
    return yaml.safe_load(open(_p("workflow.yaml"), encoding="utf-8")) or {}


# Template tier: declared recipe artifacts exist and have basic shape.
_check("artifact-workflow-exists", "workflow.yaml exists", lambda: os.path.isfile(_p("workflow.yaml")))
_check("artifact-workflow-non-empty", "workflow.yaml non-empty",
       lambda: os.path.isfile(_p("workflow.yaml")) and os.path.getsize(_p("workflow.yaml")) > 0)
_check("artifact-workflow-yaml-parses", "workflow.yaml parses as YAML",
       lambda: _yaml_parses(_p("workflow.yaml")))
_check("artifact-contract-exists", "artifact_contract.yaml exists",
       lambda: os.path.isfile(_p("artifact_contract.yaml")))
_check("artifact-contract-yaml-parses", "artifact_contract.yaml parses as YAML",
       lambda: _yaml_parses(_p("artifact_contract.yaml")))
_check("artifact-task-class-exists", "task_class.yaml exists", lambda: os.path.isfile(_p("task_class.yaml")))
_check("artifact-task-class-yaml-parses", "task_class.yaml parses as YAML",
       lambda: _yaml_parses(_p("task_class.yaml")))


def _prompts_dir_non_empty():
    d = _p("prompts")
    if not os.path.isdir(d):
        return False
    return any(f.endswith(".md") and os.path.getsize(os.path.join(d, f)) > 0
               for f in os.listdir(d))


_check("prompts-dir-non-empty", "prompts dir has non-empty md files", _prompts_dir_non_empty)


def _verifiers_dir_non_empty():
    # .py world: verifiers may be .py (ported) or .sh (deprecated but working).
    d = _p("verifiers")
    if not os.path.isdir(d):
        return False
    return any(f.endswith((".sh", ".py")) for f in os.listdir(d))


_check("verifiers-dir-non-empty", "verifiers dir has shell scripts", _verifiers_dir_non_empty)

# Task-specific tier from plan.json verifier_contract.
_check("workflow_yaml_valid", "workflow has at least one researcher node",
       lambda: any((n.get("node_type") or n.get("type")) == "researcher"
                   for n in (_workflow().get("nodes") or [])))


def _family_heterogeneity_floor():
    def fam(lane):
        m = re.match(r"^([a-z]+)", lane or "")
        return m.group(1) if m else lane
    fams = {fam(n.get("model_lane")) for n in _workflow().get("nodes", []) if n.get("model_lane")}
    assert len(fams) >= 3
    return True


_check("family_heterogeneity_floor", "at least three distinct model lanes/families",
       _family_heterogeneity_floor)


def _prompts_exist_nonempty():
    missing = []
    for n in _workflow().get("nodes", []):
        ref = n.get("prompt_ref")
        if ref and ref != "null" and not os.path.getsize(_p(ref)):
            missing.append(ref)
    assert not missing, missing
    return True


_check("prompts_exist_nonempty", "every workflow prompt_ref exists and is non-empty",
       _prompts_exist_nonempty)


def _verifiers_syntax_clean():
    refs = [n.get("verifier_ref") for n in _workflow().get("nodes", []) if n.get("verifier_ref")]
    assert refs, "no verifier_ref entries"
    bad = []
    for ref in refs:
        path = _p(ref)
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            bad.append(ref)
            continue
        # .py world: .py refs compile; .sh refs pass bash -n.
        if ref.endswith(".py"):
            rc = subprocess.run([sys.executable, "-m", "py_compile", path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        else:
            rc = subprocess.run(["bash", "-n", path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if rc:
            bad.append(ref)
    assert not bad, bad
    return True


_check("verifiers_exist_executable_syntax_clean",
       "every workflow verifier_ref exists, is executable, and passes bash -n",
       _verifiers_syntax_clean)


def _artifact_contract_declared():
    d = yaml.safe_load(open(_p("artifact_contract.yaml"), encoding="utf-8")) or {}
    assert d.get("source_artifact") and d.get("outputs")
    return True


_check("artifact_contract_declared", "artifact_contract.yaml declares source_artifact and outputs[]",
       _artifact_contract_declared)


def _task_class_keywords_min3():
    d = yaml.safe_load(open(_p("task_class.yaml"), encoding="utf-8")) or {}
    assert len(((d.get("matches") or {}).get("keywords") or [])) >= 3
    return True


_check("task_class_keywords_min3", "task_class.yaml declares at least three matcher keywords",
       _task_class_keywords_min3)


def _reviewer_implementer_family_split():
    nodes = _workflow().get("nodes") or []
    reviewers = [n.get("model_lane") for n in nodes if (n.get("node_type") or n.get("type")) == "reviewer"]
    impls = [n.get("model_lane") for n in nodes
             if (n.get("node_type") or n.get("type")) == "implementer"
             and (n.get("id") or n.get("name")) != "verifier_smith"]
    assert reviewers and impls and all(r != i for r in reviewers for i in impls)
    return True


_check("reviewer_implementer_family_split", "reviewer model lane differs from implementer model lane",
       _reviewer_implementer_family_split)


def _verifier_output_contract_nonempty():
    paths = glob.glob(os.path.join(RUN_DIR, "static-check.json")) + \
        glob.glob(os.path.join(RUN_DIR, "test-verifier.json"))
    if not paths:
        raise AssertionError("no verifier output JSON files found")
    for path in paths:
        d = json.load(open(path))
        assert d.get("checks"), path
        assert isinstance(d.get("reasons"), list), path
    return True


_check("verifier_output_contract_nonempty", "static/test verifier outputs have non-empty checks[] and reasons[]",
       _verifier_output_contract_nonempty)


def _reviewer_verdict_enum():
    assert json.load(open(os.path.join(RUN_DIR, "review-opus_arbiter.json"))).get("verdict") \
        in {"approve", "revise", "reject"}
    return True


_check("reviewer_verdict_enum", "review-opus_arbiter.json has verdict approve|revise|reject",
       _reviewer_verdict_enum)


def _recipe_validator_self_test():
    if RECIPE_DIR in (os.path.join(REPO_ROOT, "recipes", "code-fix"), "recipes/code-fix"):
        return True
    env = {**os.environ, "MINI_ORK_RUN_DIR": RUN_DIR, "MINI_ORK_ROOT": REPO_ROOT}
    out = "/tmp/framework-edit-recipe-validator-self-test.json"
    with open(out, "w") as fh:
        rc = subprocess.run([sys.executable, os.path.abspath(__file__),
                             os.path.join(REPO_ROOT, "recipes", "code-fix")],
                            stdout=fh, env=env).returncode
    if rc != 0:
        return False
    json.load(open(out))
    return True


_check("recipe_validator_self_test", "validator can run against known-good recipes/code-fix without NameError",
       _recipe_validator_self_test)

checks = []
with open(CHECKS_TSV) as f:
    for line in f:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"name": cid, "expected": desc, "actual": "see evidence log",
                       "pass": passed == "true"})
failed = [c["name"] for c in checks if not c["pass"]]
print(json.dumps({
    "verifier": NAME,
    "pass": not failed,
    "evidence_path": EVIDENCE,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": [f"{c['name']} failed; see {EVIDENCE}" for c in checks if not c["pass"]],
    "artifact_ref": RECIPE_DIR,
}))

_ev.close()
_tsv.close()
sys.exit(0)
