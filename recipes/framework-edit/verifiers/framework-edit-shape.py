#!/usr/bin/env python3
# verifiers/framework-edit-shape.py — validate the framework-edit recipe tree.
#
# Python port of framework-edit-shape.sh (bash-removal WS8). Same checks,
# evidence text, JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory set by the native execute runtime
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

import json
import os
import re
import subprocess
import sys

import yaml

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
NAME = "framework-edit-shape"
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")

open(CHECKS_TSV, "w").close()
_ev = open(EVIDENCE, "w")
_tsv = open(CHECKS_TSV, "a")

if os.path.isdir(os.path.join(RUN_DIR, "chosen", "framework-edit")):
    RECIPE_DIR = os.path.join(RUN_DIR, "chosen", "framework-edit")
elif os.path.isdir(os.path.join(os.environ.get("MINI_ORK_ROOT") or os.getcwd(), "recipes", "framework-edit")):
    RECIPE_DIR = os.path.join(os.environ.get("MINI_ORK_ROOT") or os.getcwd(), "recipes", "framework-edit")
else:
    RECIPE_DIR = "recipes/framework-edit"


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


def _prompts_non_empty():
    d = _p("prompts")
    if not os.path.isdir(d):
        return False
    return any(f.endswith(".md") and os.path.getsize(os.path.join(d, f)) > 0
               for f in os.listdir(d))


# Template tier: declared recipe artifacts exist, are non-empty, and parse.
_check("artifact-workflow-exists", "workflow.yaml exists", lambda: os.path.isfile(_p("workflow.yaml")))
_check("artifact-workflow-non-empty", "workflow.yaml is non-empty",
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
_check("artifact-example-kickoff-exists", "example-kickoff.md exists and is non-empty",
       lambda: os.path.isfile(_p("example-kickoff.md")) and os.path.getsize(_p("example-kickoff.md")) > 0)
_check("artifact-prompts-non-empty", "prompts/*.md files exist and are non-empty", _prompts_non_empty)


# Task-specific tier from plan.json verifier_contract.
def _workflow():
    return yaml.safe_load(open(_p("workflow.yaml"), encoding="utf-8")) or {}


_check("workflow_yaml_parses", "workflow has at least one researcher node",
       lambda: any((n.get("node_type") or n.get("type")) == "researcher"
                   for n in (_workflow().get("nodes") or [])))


def _no_meta_node_leakage():
    names = {n.get("id") or n.get("name") for n in _workflow().get("nodes", [])}
    forbidden = {"opus_arbiter", "verifier_smith", "glm_drafter", "kimi_drafter", "codex_drafter"}
    leaked = sorted(names & forbidden)
    assert not leaked, leaked
    return True


_check("no_meta_node_leakage", "derived workflow contains no meta-recipe node names", _no_meta_node_leakage)
_check("exact_nine_nodes", "workflow has exactly 9 nodes",
       lambda: len(_workflow().get("nodes") or []) == 9)


def _heterogeneity_floor():
    ignored = {"verifier", "publisher", "rollback", "decomposer"}
    families = set()
    for n in _workflow().get("nodes", []):
        lane = n.get("model_lane")
        if not lane or lane in ignored:
            continue
        m = re.match(r"^([a-z]+)", lane)
        families.add(m.group(1) if m else lane)
    assert len(families) >= 3, families
    return True


_check("heterogeneity_floor", "workflow uses at least three distinct model families", _heterogeneity_floor)


def _lane_bindings_verbatim():
    actual = {n.get("id") or n.get("name"): n.get("model_lane") for n in _workflow().get("nodes", [])}
    expected = {
        "planner": "decomposer",
        "code_impact_lens": "kimi_lens",
        "prior_art_lens": "codex_lens",
        "implementer": "glm_lens",
        "static_check_verifier": "verifier",
        "test_verifier": "verifier",
        "reviewer": "opus_lens",
        "publisher": "publisher",
        "rollback": "rollback",
    }
    assert actual == expected, {"actual": actual, "expected": expected}
    return True


_check("lane_bindings_verbatim", "lane assignments match kickoff binding exactly", _lane_bindings_verbatim)


def _prompts_exist_nonempty():
    bad = []
    for n in _workflow().get("nodes", []):
        ref = n.get("prompt_ref")
        if ref and ref != "null":
            path = _p(ref)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                bad.append(ref)
    assert not bad, bad
    return True


_check("prompts_exist_nonempty", "all referenced prompt_ref files exist and are non-empty",
       _prompts_exist_nonempty)


def _verifiers_clean():
    # Ported to the .py world: .py verifier_refs compile, .sh refs pass bash -n.
    bad = []
    for n in _workflow().get("nodes", []):
        ref = n.get("verifier_ref")
        if not ref:
            continue
        path = _p(ref)
        if not os.path.isfile(path):
            bad.append(ref)
            continue
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


_check("verifiers_executable_clean", "referenced verifiers exist and pass bash -n", _verifiers_clean)


def _contract_declares_outputs():
    d = yaml.safe_load(open(_p("artifact_contract.yaml"), encoding="utf-8")) or {}
    outs = " ".join(map(str, d.get("outputs") or []))
    assert d.get("source_artifact"), d
    assert "framework-edit.diff" in outs and "verdict.json" in outs, outs
    return True


_check("artifact_contract_declares_outputs",
       "artifact_contract declares source_artifact and outputs including required artifacts",
       _contract_declares_outputs)
_check("task_class_keywords", "task_class.yaml declares at least three keywords",
       lambda: len(((yaml.safe_load(open(_p("task_class.yaml"), encoding="utf-8")) or {})
                    .get("matches") or {}).get("keywords") or []) >= 3)


def _grep_in(pattern, *paths, flags=0):
    for path in paths:
        if os.path.isdir(path):
            for dirpath, _dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(dirpath, f)
                    try:
                        if re.search(pattern, open(fp, encoding="utf-8", errors="replace").read(), flags):
                            return True
                    except OSError:
                        continue
        elif os.path.isfile(path):
            if re.search(pattern, open(path, encoding="utf-8", errors="replace").read(), flags):
                return True
    return False


_check("verdict_schema_verbatim", "prompts document verdict keys in binding order",
       lambda: _grep_in(r"files_changed.*tests_pass.*static_pass.*pass",
                        _p("prompts"), _p("example-kickoff.md")))
_check("verifier_payload_structured", "review prompt requires structured verifier/reviewer payload fields",
       lambda: (_grep_in(r"reasons", _p("prompts"))
                and _grep_in(r"checked_criteria", _p("prompts"))
                and _grep_in(r"artifact_ref", _p("prompts"))))
_check("example_kickoff_exists", "example kickoff describes a realistic two-file mini-ork edit",
       lambda: (_grep_in(r"two files", _p("example-kickoff.md"), flags=re.I)
                and _grep_in(r"Trajectory", _p("example-kickoff.md"), flags=re.I)))

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
    "verdict": "pass" if not failed else "fail",
    "evidence_path": EVIDENCE,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "reasons": [f"{c['name']} failed; see {EVIDENCE}" for c in checks if not c["pass"]],
    "checked_criteria": [c["name"] for c in checks],
    "artifact_ref": RECIPE_DIR,
}))

_ev.close()
_tsv.close()
sys.exit(0)
