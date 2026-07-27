#!/usr/bin/env python3
# verifiers/recipe-validator.py - static validation for the researcher-qdrant-contract recipe tree.
#
# Python port of recipe-validator.sh (bash-removal WS8). Same checks, evidence
# text, JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory set by the native execute runtime
#   MINI_ORK_ROOT    - optional mini-ork repo root
#
# Output: JSON to stdout. Exit code is always 0; caller reads .pass.

import json
import os
import pathlib
import re
import subprocess
import sys

import yaml

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
REPO_ROOT = os.environ.get("MINI_ORK_ROOT") or os.getcwd()
NAME = "recipe-validator"
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")

open(CHECKS_TSV, "w").close()
_ev = open(EVIDENCE, "w")
_tsv = open(CHECKS_TSV, "a")

if os.path.isdir(os.path.join(RUN_DIR, "chosen", "researcher-qdrant-contract")):
    RECIPE_DIR = os.path.join(RUN_DIR, "chosen", "researcher-qdrant-contract")
elif os.path.isdir(os.path.join(REPO_ROOT, "recipes", "researcher-qdrant-contract")):
    RECIPE_DIR = os.path.join(REPO_ROOT, "recipes", "researcher-qdrant-contract")
else:
    RECIPE_DIR = "recipes/researcher-qdrant-contract"


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


def _check_fatal(cid, desc, fn):
    """The .sh ran these loop-conditions via ``eval`` with ``|| exit 1`` INSIDE
    the eval — a failure exits the whole script immediately (rc 1, no JSON,
    no TSV entry). Replicate that exactly."""
    _ev.write(f"[{cid}] {desc}\n")
    _ev.flush()
    if not fn():
        sys.exit(1)
    _tsv.write(f"{cid}\t{desc}\ttrue\n")
    _tsv.flush()
    _ev.write("  ok\n")
    _ev.flush()


def _p(*parts):
    return os.path.join(RECIPE_DIR, *parts)


def _read(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def _grep(pattern, path, flags=0):
    return re.search(pattern, _read(path), flags) is not None


def _nonempty(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _yaml_parses(path):
    yaml.safe_load(open(path, encoding="utf-8"))
    return True


def _verifier_files():
    """Verifier scripts in the recipe — .py (ported) or .sh (deprecated)."""
    d = _p("verifiers")
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith((".sh", ".py")))


def _prompt_files():
    d = _p("prompts")
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md"))


# Template tier: declared recipe artifacts exist, are non-empty, and have shape.
_check("workflow-exists", "workflow.yaml exists", lambda: os.path.isfile(_p("workflow.yaml")))
_check("workflow-non-empty", "workflow.yaml is non-empty", lambda: _nonempty(_p("workflow.yaml")))
_check("workflow-yaml-parses", "workflow.yaml parses as YAML", lambda: _yaml_parses(_p("workflow.yaml")))
_check("workflow-has-nodes-anchor", "workflow.yaml declares nodes anchor",
       lambda: _grep(r"^nodes:", _p("workflow.yaml"), re.M))

_check("artifact-contract-exists", "artifact_contract.yaml exists", lambda: os.path.isfile(_p("artifact_contract.yaml")))
_check("artifact-contract-non-empty", "artifact_contract.yaml is non-empty", lambda: _nonempty(_p("artifact_contract.yaml")))
_check("artifact-contract-yaml-parses", "artifact_contract.yaml parses as YAML",
       lambda: _yaml_parses(_p("artifact_contract.yaml")))
_check("artifact-contract-has-outputs-anchor", "artifact_contract.yaml declares outputs anchor",
       lambda: _grep(r"^outputs:", _p("artifact_contract.yaml"), re.M))

_check("task-class-exists", "task_class.yaml exists", lambda: os.path.isfile(_p("task_class.yaml")))
_check("task-class-non-empty", "task_class.yaml is non-empty", lambda: _nonempty(_p("task_class.yaml")))
_check("task-class-yaml-parses", "task_class.yaml parses as YAML", lambda: _yaml_parses(_p("task_class.yaml")))
_check("readme-non-empty", "README.md exists and is non-empty", lambda: _nonempty(_p("README.md")))
_check("example-kickoff-non-empty", "example-kickoff.md exists and is non-empty",
       lambda: _nonempty(_p("example-kickoff.md")))
_check("prompts-exist", "prompts/*.md files exist", lambda: len(_prompt_files()) > 0)
_check_fatal("prompts-non-empty", "all prompts/*.md files are non-empty",
             lambda: all(os.path.getsize(f) > 0 for f in _prompt_files()) and len(_prompt_files()) > 0)
_check_fatal("prompts-have-headings", "all prompts/*.md files have markdown heading anchors",
             lambda: all(_grep(r"^# ", f, re.M) for f in _prompt_files()) and len(_prompt_files()) > 0)
_check("verifiers-exist", "verifiers/*.sh files exist", lambda: len(_verifier_files()) > 0)
_check_fatal("verifiers-non-empty", "all verifiers/*.sh files are non-empty",
             lambda: all(os.path.getsize(f) > 0 for f in _verifier_files()) and len(_verifier_files()) > 0)


def _verifiers_syntax():
    # .py world: .py verifiers compile; .sh verifiers pass bash -n.
    for f in _verifier_files():
        if f.endswith(".py"):
            rc = subprocess.run([sys.executable, "-m", "py_compile", f],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        else:
            rc = subprocess.run(["bash", "-n", f],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if rc != 0:
            return False
    return True


_check_fatal("verifiers-bash-syntax", "all verifiers/*.sh pass bash -n", _verifiers_syntax)
_check("evidence-log-opened", "evidence log was opened for writing", lambda: os.path.isfile(EVIDENCE))


# Task-specific tier from plan.json verifier_contract.
def _workflow():
    return yaml.safe_load(open(_p("workflow.yaml"), encoding="utf-8")) or {}


_check("workflow-researcher-present", "workflow declares at least one researcher node",
       lambda: any((n.get("node_type") or n.get("type")) == "researcher"
                   for n in (_workflow().get("nodes") or [])))


def _three_model_families():
    lanes = [n.get("model_lane") for n in (_workflow().get("nodes") or []) if n.get("model_lane")]
    fam = {re.sub(r"_(lens|drafter)$", "", x) for x in lanes}
    assert len(fam) >= 3, fam
    return True


_check("three-model-families", "workflow declares at least three distinct model lanes",
       _three_model_families)


def _four_lens_responsibilities():
    blob = str(_workflow()).lower()
    missing = [t for t in ("contract", "creation", "retriev", "backfill") if t not in blob]
    assert not missing, missing
    return True


_check("four-lens-responsibilities", "workflow includes contract, creation, retrieval, and backfill lenses",
       _four_lens_responsibilities)


def _no_meta_node_leakage():
    blob = str(_workflow()).lower()
    forbidden = ("opus_arbiter", "verifier_smith", "glm_drafter", "kimi_drafter",
                 "codex_drafter", "drafter_glm", "drafter_kimi", "drafter_codex")
    leaked = [x for x in forbidden if x in blob]
    assert not leaked, leaked
    return True


_check("no-meta-node-leakage-workflow", "workflow contains no meta-recipe node names",
       _no_meta_node_leakage)


def _contract_declares_outputs():
    d = yaml.safe_load(open(_p("artifact_contract.yaml"), encoding="utf-8")) or {}
    assert "source_artifact" in d and isinstance(d.get("outputs"), list) and d["outputs"]
    return True


_check("artifact-contract-declares-outputs", "artifact_contract.yaml declares source_artifact and outputs[]",
       _contract_declares_outputs)


def _task_class_keywords():
    d = yaml.safe_load(open(_p("task_class.yaml"), encoding="utf-8")) or {}
    kw = d.get("matches", {}).get("keywords", [])
    assert len(kw) >= 3, kw
    return True


_check("task-class-keywords-min", "task_class.yaml declares at least three keywords", _task_class_keywords)


def _required_outputs_named():
    blob = str(yaml.safe_load(open(_p("artifact_contract.yaml"), encoding="utf-8")) or {})
    req = ("qdrant-contract-remediation-plan.md", "qdrant-contract-findings.json",
           "qdrant-contract-patch-summary.md", "qdrant-contract-verification.md")
    missing = [x for x in req if x not in blob]
    assert not missing, missing
    return True


_check("required-output-artifacts-named", "artifact_contract names the four required outputs",
       _required_outputs_named)
_check("findings-envelope-contract-documented",
       "planner, implementer, and reviewer agree on findings envelope shape",
       lambda: all(_grep(r"findings", _p("prompts", f"{n}.md"))
                   and _grep(r"metadata", _p("prompts", f"{n}.md"))
                   for n in ("planner", "implementer", "reviewer")))


def _grep_prompts(pattern, flags=0):
    return any(_grep(pattern, f, flags) for f in _prompt_files())


_check("no-raw-array-findings-verifier", "recipe prompts do not validate findings with raw .[] iteration",
       lambda: not _grep_prompts(r"all\(\.\[\]"))
_check("planner-does-not-call-findings-raw-array",
       "planner artifact manifest does not call findings a raw JSON array",
       lambda: not _grep(r"Machine-readable JSON array", _p("prompts", "planner.md")))
_check("payload-contract-keys-documented", "prompts document text_preview and source_kind",
       lambda: _grep_prompts(r"text_preview") and _grep_prompts(r"source_kind"))
_check("reconciliation-dry-run-flag", "prompts require --dry-run support",
       lambda: _grep_prompts(r"--dry-run"))


def _grep_verifiers(pattern, flags=0):
    return any(_grep(pattern, f, flags) for f in _verifier_files())


_check("no-new-direct-qdrant-writer-guard", "verifiers grep-deny direct Qdrant writers",
       lambda: _grep_verifiers(r"upsert|upload_points")
       and _grep_verifiers(r"deny|forbid|reject|no-new-direct", re.I))


def _no_network_calls():
    root = pathlib.Path(_p("verifiers"))
    pat = re.compile(r"(^|[;&| \t])(curl|wget|nc|telnet|ssh|scp|rsync)([ \t]|$)")
    bad = []
    for p in list(root.glob("*.sh")) + list(root.glob("*.py")):
        for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if "pat=re.compile" in line or "no-network-calls-in-verifiers" in line:
                continue
            if pat.search(line):
                bad.append(f"{p}:{i}")
    assert not bad, bad
    return True


_check("no-network-calls-in-verifiers", "verifiers do not call network tools", _no_network_calls)


def _verifier_contract_fields():
    for f in _verifier_files():
        text = _read(f)
        if not ('"status"' in text and '"checks"' in text and '"reviewer_verdict"' in text):
            return False
    return True


_check_fatal("verifier-contract-fields", "verifiers emit status, checks, and reviewer_verdict fields",
             _verifier_contract_fields)

checks = []
with open(CHECKS_TSV, encoding="utf-8") as fh:
    for line in fh:
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({
            "name": cid,
            "passed": passed == "true",
            "evidence": f"{EVIDENCE}#{cid}",
            "description": desc,
        })
failed = [c["name"] for c in checks if not c["passed"]]
status = "pass" if not failed else "fail"
print(json.dumps({
    "verifier": NAME,
    "status": status,
    "pass": not failed,
    "reviewer_verdict": status,
    "evidence_path": EVIDENCE,
    "checks_run": [c["name"] for c in checks],
    "failed_checks": failed,
    "checks": checks,
    "files_read": [
        f"{RECIPE_DIR}/workflow.yaml",
        f"{RECIPE_DIR}/artifact_contract.yaml",
        f"{RECIPE_DIR}/task_class.yaml",
        f"{RECIPE_DIR}/prompts/*.md",
        f"{RECIPE_DIR}/verifiers/*.sh",
    ],
    "tool_calls": ["bash", "python3", "grep", "find"],
    "duration_ms": 1,
}, sort_keys=True))

_ev.close()
_tsv.close()
sys.exit(0)
