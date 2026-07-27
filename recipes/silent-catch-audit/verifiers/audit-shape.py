#!/usr/bin/env python3
# verifiers/audit-shape.py - validate silent-catch-audit recipe shape and outputs.
#
# Python port of audit-shape.sh (bash-removal WS8). Same checks, evidence text,
# JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR - run directory (set by the native execute runtime)
#
# Output: JSON to stdout
#   { "verifier": "audit-shape", "pass": bool, "evidence_path": "...",
#     "checks_run": [...], "failed_checks": [...] }
# Exit codes: always 0 (caller reads .pass from JSON).

import json
import os
import pathlib
import re
import subprocess
import sys

import yaml

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.dirname(SCRIPT_DIR)
EVIDENCE = os.path.join(RUN_DIR, "verifier-audit-shape.log")
ev = open(EVIDENCE, "w")

checks_run = []
failed_checks = []


def _check(cid, expr_desc, fn):
    checks_run.append(cid)
    ev.write(f"[{cid}] {expr_desc}\n")
    ev.flush()
    try:
        ok = bool(fn())
    except Exception as exc:
        ev.write(f"{type(exc).__name__}: {exc}\n")
        ok = False
    ev.write("  ok\n" if ok else "  FAIL\n")
    ev.flush()
    if not ok:
        failed_checks.append(cid)


def _p(*parts):
    return os.path.join(RECIPE_DIR, *parts)


def _nonempty(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _grep(pattern, path, flags=0):
    try:
        return re.search(pattern, open(path, encoding="utf-8", errors="replace").read(), flags) is not None
    except OSError:
        return False


# Template tier: declared recipe artifacts exist and have basic shape.
_check("artifact-workflow-exists", "workflow.yaml exists and is non-empty",
       lambda: _nonempty(_p("workflow.yaml")))
_check("artifact-contract-exists", "artifact_contract.yaml exists and is non-empty",
       lambda: _nonempty(_p("artifact_contract.yaml")))
_check("artifact-task-class-exists", "task_class.yaml exists and is non-empty",
       lambda: _nonempty(_p("task_class.yaml")))
_check("artifact-readme-exists", "README.md exists and is non-empty",
       lambda: _nonempty(_p("README.md")))
_check("artifact-kickoff-exists", "example-kickoff.md exists and is non-empty",
       lambda: _nonempty(_p("example-kickoff.md")))


def _prompts_exist():
    prompt_dir = pathlib.Path(_p("prompts"))
    files = sorted(prompt_dir.glob("*.md"))
    assert files, "no prompt markdown files"
    empty = [str(p.name) for p in files if p.stat().st_size == 0]
    assert not empty, "empty prompt files: " + ", ".join(empty)
    return True


_check("artifact-prompts-exist", "prompts/*.md contains non-empty prompt files", _prompts_exist)
# .py world: the verifier itself is the ported .py sibling.
_check("artifact-verifiers-exist", "verifiers/*.sh contains this verifier",
       lambda: _nonempty(_p("verifiers", "audit-shape.py")))


def _yaml_files_parse():
    root = pathlib.Path(RECIPE_DIR)
    for name in ("workflow.yaml", "artifact_contract.yaml", "task_class.yaml"):
        data = yaml.safe_load((root / name).read_text())
        assert isinstance(data, dict), f"{name} did not parse to a mapping"
    return True


_check("yaml-files-parse", "workflow, artifact contract, and task class parse as YAML", _yaml_files_parse)
_check("markdown-shape", "README and kickoff expose expected headings",
       lambda: _grep(r"^# ", _p("README.md"), re.M) and _grep(r"^# ", _p("example-kickoff.md"), re.M))


# Task-specific tier: mechanize verifier_contract.checks[] from plan.json.
def _workflow_yaml_parses():
    w = yaml.safe_load(open(_p("workflow.yaml"), encoding="utf-8"))
    nodes = w.get("nodes", [])
    assert isinstance(nodes, list), "nodes must be a list"
    assert any(n.get("type") == "researcher" for n in nodes), "no researcher node"
    return True


_check("workflow_yaml_parses", "workflow.yaml declares at least one researcher node", _workflow_yaml_parses)


def _heterogeneity_floor():
    w = yaml.safe_load(open(_p("workflow.yaml"), encoding="utf-8"))
    families = {
        str(n["model_lane"]).rsplit("_", 1)[0]
        for n in w.get("nodes", [])
        if n.get("model_lane")
    }
    assert len(families) >= 3, f"only {len(families)} families: {sorted(families)}"
    return True


_check("heterogeneity_floor", "workflow has at least three distinct model families", _heterogeneity_floor)


def _prompt_refs_resolve():
    root = pathlib.Path(RECIPE_DIR)
    w = yaml.safe_load((root / "workflow.yaml").read_text())
    missing = []
    for node in w.get("nodes", []):
        ref = node.get("prompt_ref")
        if ref:
            path = root / ref
            if not path.is_file() or path.stat().st_size == 0:
                missing.append(ref)
    assert not missing, "missing or empty prompt refs: " + ", ".join(missing)
    return True


_check("prompt_refs_resolve", "all workflow prompt_ref entries resolve to non-empty prompts",
       _prompt_refs_resolve)


def _verifier_refs_resolve():
    # .py world: .py verifier_refs compile; .sh refs pass bash -n.
    root = pathlib.Path(RECIPE_DIR)
    w = yaml.safe_load((root / "workflow.yaml").read_text())
    bad = []
    for node in w.get("nodes", []):
        ref = node.get("verifier_ref")
        if not ref:
            continue
        path = root / ref
        if not path.is_file() or path.stat().st_size == 0:
            bad.append(f"{ref}: missing or empty")
            continue
        if ref.endswith(".py"):
            result = subprocess.run([sys.executable, "-m", "py_compile", str(path)],
                                    text=True, capture_output=True)
        else:
            result = subprocess.run(["bash", "-n", str(path)], text=True, capture_output=True)
        if result.returncode != 0:
            bad.append(f"{ref}: syntax check failed: {result.stderr.strip()}")
    assert not bad, "; ".join(bad)
    return True


_check("verifier_refs_resolve", "all workflow verifier_ref entries resolve and pass bash -n",
       _verifier_refs_resolve)


def _contract_declares_outputs():
    a = yaml.safe_load(open(_p("artifact_contract.yaml"), encoding="utf-8"))
    assert a.get("source_artifact"), "missing source_artifact"
    outputs = a.get("outputs")
    assert isinstance(outputs, list) and len(outputs) > 0, "missing outputs"
    return True


_check("artifact_contract_declares_outputs", "artifact_contract.yaml declares source_artifact and outputs[]",
       _contract_declares_outputs)


def _task_class_keywords():
    t = yaml.safe_load(open(_p("task_class.yaml"), encoding="utf-8"))
    kw = t.get("matches", {}).get("keywords", [])
    assert isinstance(kw, list) and len(kw) >= 3, f"only {len(kw)} keywords"
    return True


_check("task_class_keywords", "task_class.yaml declares at least three match keywords", _task_class_keywords)


# Epic-output guards: active only after the recipe has produced run-local outputs.
def _grep_r(needle, root):
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                if needle in open(os.path.join(dirpath, f), encoding="utf-8", errors="replace").read():
                    return True
            except OSError:
                continue
    return False


_check("epic-output-contract", "prompts and contract declare markdown and JSON audit outputs",
       lambda: _grep_r("silent-catch-audit.md", RECIPE_DIR)
       and _grep_r("silent-catch-audit.findings.json", RECIPE_DIR))


def _epic_output_shape_if_present():
    run = pathlib.Path(RUN_DIR)
    md = run / "silent-catch-audit.md"
    js = run / "silent-catch-audit.findings.json"
    if not md.exists() and not js.exists():
        return True
    assert md.is_file() and md.stat().st_size > 0, "missing markdown output"
    assert js.is_file() and js.stat().st_size > 0, "missing findings JSON"
    text = md.read_text(errors="replace")
    assert re.search(r"\b(pass|fail)\b", text, re.I), "markdown missing pass/fail verdict"
    for label in ("Critical", "High", "Medium", "Low", "Allowed"):
        assert re.search(rf"\b{label}\b", text), f"markdown missing {label} tier"
    data = json.loads(js.read_text())
    verdict = data.get("verdict")
    assert verdict in {"pass", "fail"}, "JSON verdict must be pass or fail"
    summary = data.get("summary", {})
    findings = data.get("findings", [])
    assert isinstance(summary, dict), "summary must be object"
    assert isinstance(findings, list), "findings must be list"
    for sev in ("critical", "high", "medium", "low", "allowed"):
        count = summary.get(sev)
        assert isinstance(count, int) and count >= 0, f"summary.{sev} must be non-negative int"
    actual = {sev: 0 for sev in ("critical", "high", "medium", "low", "allowed")}
    for item in findings:
        sev = str(item.get("severity", "")).lower()
        if sev in actual:
            actual[sev] += 1
    for sev, count in actual.items():
        assert summary.get(sev) == count, f"summary.{sev}={summary.get(sev)} but findings have {count}"
    assert (verdict == "fail") == (summary.get("critical", 0) > 0), "verdict must fail iff critical > 0"
    return True


_check("epic-output-shape-if-present", "if audit outputs exist, markdown/JSON verdict shape is coherent",
       _epic_output_shape_if_present)


def _read_only_boundary():
    a = yaml.safe_load(open(_p("artifact_contract.yaml"), encoding="utf-8"))
    for out in a.get("outputs", []):
        p = pathlib.PurePosixPath(str(out))
        assert p.suffix not in {".ts", ".tsx", ".js", ".jsx"}, f"source output forbidden: {out}"
        assert p.name not in {".eslintrc", ".eslintrc.json", "eslint.config.js", "eslint.config.mjs"}, \
            f"lint config output forbidden: {out}"
    return True


_check("read-only-boundary", "recipe does not emit source mutations or lint configuration outputs",
       _read_only_boundary)

passed = not failed_checks

print(json.dumps({
    "verifier": "audit-shape",
    "pass": passed,
    "evidence_path": EVIDENCE,
    "checks_run": checks_run,
    "failed_checks": failed_checks,
}))

ev.close()
sys.exit(0)
