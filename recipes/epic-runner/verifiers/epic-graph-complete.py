#!/usr/bin/env python3
# verifiers/epic-graph-complete.py — validate epic-runner recipe shape and run artifacts.
#
# Python port of epic-graph-complete.sh (bash-removal WS8). Same checks,
# evidence text, JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "epic-graph-complete", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

import json
import os
import re
import subprocess
import sys
import time

import yaml

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
NAME = "epic-graph-complete"
EVIDENCE = os.path.join(RUN_DIR, f"verifier-{NAME}.log")
CHECKS_TSV = os.path.join(RUN_DIR, f"verifier-{NAME}.checks.tsv")
START_MS = int(time.time() * 1000)

open(CHECKS_TSV, "w").close()
_ev = open(EVIDENCE, "w")
_tsv = open(CHECKS_TSV, "a")

if os.path.isdir(os.path.join(RUN_DIR, "chosen", "epic-runner")):
    RECIPE_DIR = os.path.join(RUN_DIR, "chosen", "epic-runner")
elif os.path.isdir(os.path.join(RUN_DIR, "recipes", "epic-runner")):
    RECIPE_DIR = os.path.join(RUN_DIR, "recipes", "epic-runner")
elif os.path.isdir(os.path.join(os.getcwd(), "recipes", "epic-runner")):
    RECIPE_DIR = os.path.join(os.getcwd(), "recipes", "epic-runner")
else:
    RECIPE_DIR = os.path.join(RUN_DIR, "chosen", "epic-runner")

WORKFLOW = os.path.join(RECIPE_DIR, "workflow.yaml")
TASK_CLASS = os.path.join(RECIPE_DIR, "task_class.yaml")
ARTIFACT_CONTRACT = os.path.join(RECIPE_DIR, "artifact_contract.yaml")
README = os.path.join(RECIPE_DIR, "README.md")
EXAMPLE = os.path.join(RECIPE_DIR, "example-kickoff.md")
# .py world: the verifier itself is the ported .py sibling.
SELF = os.path.join(RECIPE_DIR, "verifiers", "epic-graph-complete.py")
PLAN = os.path.join(RUN_DIR, "epic-runner-plan.json")
RESULTS = os.path.join(RUN_DIR, "epic-results.json")
AGGREGATE = os.path.join(RUN_DIR, "wave-aggregate.json")
DELIVERY = os.path.join(RUN_DIR, "epic-runner-delivery.json")
TRACE = os.environ.get("MINI_ORK_TRACE_PATH") or os.path.join(RUN_DIR, "trace.json")

if os.environ.get("MINI_ORK_HOME") and \
        os.path.isfile(os.path.join(os.environ["MINI_ORK_HOME"], "config", "agents.yaml")):
    AGENTS = os.path.join(os.environ["MINI_ORK_HOME"], "config", "agents.yaml")
elif os.path.isfile(os.path.join(RUN_DIR, ".mini-ork", "config", "agents.yaml")):
    AGENTS = os.path.join(RUN_DIR, ".mini-ork", "config", "agents.yaml")
elif os.path.isfile(os.path.join(os.getcwd(), "config", "agents.yaml")):
    AGENTS = os.path.join(os.getcwd(), "config", "agents.yaml")
else:
    AGENTS = ""


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
    msg = fn()
    if msg is not None:
        if msg:
            _ev.write(msg + "\n")
            _ev.flush()
        sys.exit(1)
    _tsv.write(f"{cid}\t{desc}\ttrue\n")
    _tsv.flush()
    _ev.write("  ok\n")
    _ev.flush()


def _nonempty(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _grep(pattern, path, flags=0):
    try:
        return re.search(pattern, open(path, encoding="utf-8", errors="replace").read(), flags) is not None
    except OSError:
        return False


# Template tier (mechanical) — always.
_check("artifact-exists", "declared recipe artifacts exist",
       lambda: all(os.path.isfile(p) for p in
                   (WORKFLOW, TASK_CLASS, ARTIFACT_CONTRACT, README, EXAMPLE, SELF)))
_check("artifact-non-empty", "declared recipe artifacts are non-empty",
       lambda: all(_nonempty(p) for p in
                   (WORKFLOW, TASK_CLASS, ARTIFACT_CONTRACT, README, EXAMPLE, SELF)))


def _yaml_shape():
    for path in (WORKFLOW, TASK_CLASS, ARTIFACT_CONTRACT):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict), f"{path} did not parse to a mapping"
    return True


_check("yaml-shape", "workflow/task_class/artifact_contract parse as YAML", _yaml_shape)


def _markdown_shape():
    for path in (README, EXAMPLE):
        text = open(path, encoding="utf-8").read()
        assert len(text.splitlines()) >= 10, f"{path} is too short"
    assert "MINI_ORK_EPIC_DOC" in open(README, encoding="utf-8").read()
    assert re.search(r"(researcher|schema-migration|scalable-schema-migration)",
                     open(EXAMPLE, encoding="utf-8").read())
    return True


_check("markdown-shape", "README and example have useful line count and anchors", _markdown_shape)
def _prompt_artifacts_fatal():
    for f in ("planner", "epic-dispatcher", "wave-aggregator", "final-reviewer"):
        if not _nonempty(os.path.join(RECIPE_DIR, "prompts", f"{f}.md")):
            return ""
    return None


_check_fatal("prompt-artifacts-shape", "required prompt artifacts exist and are non-empty",
             _prompt_artifacts_fatal)


def _self_compiles():
    return subprocess.run([sys.executable, "-m", "py_compile", SELF],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


_check("verifier-bash-n", "verifier script passes bash -n", _self_compiles)
_check("evidence-log-opened", "evidence log path is writable",
       lambda: os.path.isfile(EVIDENCE) and os.path.getsize(EVIDENCE) > 0)

# Task-specific tier (plan verifier_contract + epic-runner semantics).
def _workflow_parses():
    yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    return True


_check("workflow-yaml-parses", "workflow.yaml exists and parses as valid YAML", _workflow_parses)


def _node_count():
    d = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    nodes = d.get("nodes", [])
    assert len(nodes) == 8, f"expected 8 got {len(nodes)}"
    return True


_check("workflow-node-count-exact-8", "workflow.yaml declares exactly 8 nodes", _node_count)


def _binding_shape():
    d = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    names = {n.get("name") for n in d.get("nodes", [])}
    expected = {"planner", "epic_dispatcher", "wave_aggregator", "epic_verifier",
                "final_reviewer", "publisher", "rollback", "reflector"}
    assert names == expected, f"missing={sorted(expected - names)} extra={sorted(names - expected)}"
    return True


_check("workflow-binding-shape", "workflow node names match the kickoff binding exactly", _binding_shape)


def _no_meta_recipe_leakage():
    # grep -R --exclude=self -E "(...)" "$RECIPE_DIR" — pass when NO match.
    pat = re.compile(r"(arxiv_lens|prior_art_lens|glm_drafter|kimi_drafter|codex_drafter|verifier_smith|opus_arbiter|recipe_validator)")
    for dirpath, dirs, files in os.walk(RECIPE_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f in ("epic-graph-complete.sh", "epic-graph-complete.py") or f.endswith(".pyc"):
                continue
            try:
                if pat.search(open(os.path.join(dirpath, f), encoding="utf-8", errors="replace").read()):
                    return False
            except OSError:
                continue
    return True


_check("no-meta-recipe-leakage", "recipe files do not contain recipe-creator node names",
       _no_meta_recipe_leakage)


def _heterogeneity():
    d = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    lanes = {}
    if AGENTS:
        agents = yaml.safe_load(open(AGENTS, encoding="utf-8")) or {}
        lanes = agents.get("lanes", {}) or {}
    operational = {"planner", "verifier", "publisher", "rollback", "reflector"}
    families = set()
    for node in d.get("nodes", []):
        lane = node.get("model_lane")
        if not lane or lane in operational:
            continue
        family = lanes.get(lane, lane)
        if family not in operational:
            families.add(str(family))
    assert len(families) >= 3, f"expected >=3 families got {sorted(families)}"
    return True


_check("heterogeneity-3-families", "at least 3 distinct model families across non-operational model lanes",
       _heterogeneity)


def _prompts_nonempty_fatal():
    for f in ("planner", "epic-dispatcher", "wave-aggregator", "final-reviewer"):
        if not _nonempty(os.path.join(RECIPE_DIR, "prompts", f"{f}.md")):
            return f"missing or empty: {f}.md"
    return None


_check_fatal("prompts-files-exist-nonempty", "required prompt files exist and are non-empty",
             _prompts_nonempty_fatal)


def _verifier_script_ok():
    return _nonempty(SELF) and _self_compiles()


_check("verifier-script-exists-and-bash-n-clean", "epic-graph-complete.sh exists and passes bash -n",
       _verifier_script_ok)


def _contract_empty_outputs():
    d = yaml.safe_load(open(ARTIFACT_CONTRACT, encoding="utf-8"))
    assert d.get("outputs") == [], f"expected outputs: [] got {d.get('outputs')}"
    return True


_check("artifact-contract-declares-empty-outputs", "artifact_contract.yaml declares outputs: []",
       _contract_empty_outputs)


def _task_class_keywords():
    d = yaml.safe_load(open(TASK_CLASS, encoding="utf-8"))
    kw = set(d["matches"]["keywords"])
    req = {"deliver epic", "multi-epic", "schema migration", "epic runner", "epic doc"}
    assert req <= kw, f"missing keywords: {sorted(req - kw)}"
    return True


_check("task-class-keywords-present", "task_class.yaml includes all kickoff-required keywords",
       _task_class_keywords)
_check("example-kickoff-references-researcher-doc", "example kickoff references researcher schema-migration use case",
       lambda: _grep(r"(researcher|schema-migration|scalable-schema-migration)", EXAMPLE))


def _readme_env_vars_fatal():
    for v in ("MINI_ORK_EPIC_DOC", "MINI_ORK_EPIC_TARGET_REPO", "MINI_ORK_EPIC_PUBLISH",
              "MINI_ORK_EPIC_VERIFIER_SCRIPT"):
        if not _grep(re.escape(v), README):
            return f"missing env var docs: {v}"
    return None


_check_fatal("readme-documents-env-vars", "README documents all epic-runner env vars",
             _readme_env_vars_fatal)


def _publisher_evidence():
    publish = os.environ.get("MINI_ORK_EPIC_PUBLISH", "false").lower()
    if publish != "true":
        return True
    assert os.path.exists(TRACE), f"trace not found: {TRACE}"
    t = json.load(open(TRACE, encoding="utf-8"))
    nodes = t.get("nodes", [])
    pub = next((n for n in nodes if n.get("name") == "publisher"), None)
    assert pub, "publisher node missing from trace"
    assert pub.get("final_artifact_ref") and pub.get("files_written"), "publisher emitted empty evidence"
    return True


_check("publisher-evidence-non-empty", "publisher evidence is non-empty when MINI_ORK_EPIC_PUBLISH=true",
       _publisher_evidence)


def _runtime_json_parse():
    for path in (PLAN, RESULTS, AGGREGATE, DELIVERY):
        if os.path.exists(path):
            json.load(open(path, encoding="utf-8"))
    return True


_check("runtime-json-artifacts-parse-if-present", "runtime JSON artifacts parse when present",
       _runtime_json_parse)


def _all_epics_represented():
    if not (os.path.exists(PLAN) and os.path.exists(RESULTS)):
        return True
    plan = json.load(open(PLAN, encoding="utf-8"))
    results = json.load(open(RESULTS, encoding="utf-8"))
    planned = {e["id"] for e in plan.get("epics", [])}
    found = {e["id"] for e in results.get("epics", [])}
    missing = planned - found
    assert not missing, f"missing epics: {sorted(missing)}"
    return True


_check("runtime-all-epics-represented-if-present", "every planned epic appears in results when runtime artifacts exist",
       _all_epics_represented)


def _dependency_respected():
    if not os.path.exists(AGGREGATE):
        return True
    d = json.load(open(AGGREGATE, encoding="utf-8"))
    assert d.get("aggregate", {}).get("dependency_respected") is True
    return True


_check("runtime-dependency-respected-if-present", "wave aggregate reports dependency_respected=true when present",
       _dependency_respected)

END_MS = int(time.time() * 1000)
DURATION_MS = END_MS - START_MS

checks = []
with open(CHECKS_TSV, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        cid, desc, passed = line.rstrip("\n").split("\t", 2)
        checks.append({"id": cid, "description": desc, "pass": passed == "true"})
failed = [c["id"] for c in checks if not c["pass"]]
print(json.dumps({
    "verifier": NAME,
    "pass": not failed,
    "evidence_path": EVIDENCE,
    "checks_run": [c["id"] for c in checks],
    "failed_checks": failed,
    "checked_criteria": [c["id"] for c in checks],
    "artifact_ref": RECIPE_DIR,
    "duration_ms": DURATION_MS,
}, sort_keys=True))

_ev.close()
_tsv.close()
sys.exit(0)
