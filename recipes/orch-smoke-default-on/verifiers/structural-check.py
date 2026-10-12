#!/usr/bin/env python3
# verifiers/structural-check.py — pure-python structural validator for the
# orch-smoke-default-on recipe. Self-validating via __file__. Implements
# plan.json's 7 verifier_contract structural checks (workflow parse +
# researcher, 3-family heterogeneity, prompts, verifiers syntax,
# contract source/outputs, task_class keywords, rollback_strategy).
# Pure-python, no network, rc-deterministic — bash-removal (2026-07).
# Inputs:  MINI_ORK_RUN_DIR (optional; falls back to recipe dir)
# Output:  single JSON on stdout; sidecar + log to $RUN_DIR.
# Exit:    always 0 (caller reads .pass from JSON).

import json
import os
import re
import stat
import subprocess
import sys

# Recipe dir: <recipe>/verifiers/structural-check.py → parent is <recipe>/
HERE = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.dirname(HERE)
RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR") or RECIPE_DIR
EVIDENCE = os.path.join(RUN_DIR, "verifier-structural-check.log")
SIDECAR = os.path.join(RUN_DIR, "verifier-structural-check.json")

ev = open(EVIDENCE, "w")
ev.write(f"── structural-check: recipe_dir={RECIPE_DIR} ──\n")
ev.flush()

CHECK_IDS = (
    "workflow-yaml-parses", "heterogeneity-3-families",
    "prompts-exist-nonempty", "verifiers-executable-syntax",
    "contract-source-and-outputs", "task-class-keywords",
    "rollback-keeps-artifacts",
)
checks = {c: False for c in CHECK_IDS}
reasons = []


def _check(cid, desc, fn):
    ev.write(f"  [{cid}] {desc}\n"); ev.flush()
    try:
        ok = bool(fn())
    except Exception as exc:
        ev.write(f"    {type(exc).__name__}: {exc}\n")
        ok = False
    ev.write("    ok\n" if ok else "    FAIL\n")
    ev.flush()
    checks[cid] = ok
    if not ok:
        reasons.append(f"{cid}: {desc}")
    return ok


# PyYAML is a runtime dep of mini-ork; guard so a missing install
# fails closed with a clear message rather than an ImportError stack.
try:
    import yaml  # type: ignore
except ImportError as exc:
    ev.write(f"FAIL: PyYAML not importable: {exc}\n"); ev.close()
    result = {"verifier": "structural-check", "pass": False,
              "evidence_path": EVIDENCE, "sidecar_path": SIDECAR,
              "checks": checks,
              "reasons": ["yaml-unavailable: PyYAML is required"],
              "recipe_dir": RECIPE_DIR}
    open(SIDECAR, "w", encoding="utf-8").write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    sys.exit(0)


def _load(path):
    return yaml.safe_load(open(path, encoding="utf-8"))


# --- 1. workflow-yaml-parses --------------------------------------------
def check_workflow_parses():
    p = os.path.join(RECIPE_DIR, "workflow.yaml")
    if not os.path.isfile(p):
        ev.write(f"    missing: {p}\n"); return False
    wf = _load(p)
    if not isinstance(wf, dict):
        ev.write(f"    root is {type(wf).__name__}, not mapping\n"); return False
    nodes = wf.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        ev.write("    no nodes[]\n"); return False
    rs = [n for n in nodes if isinstance(n, dict) and n.get("type") == "researcher"]
    if len(rs) < 1:
        ev.write(f"    no researcher nodes (got {len(rs)})\n"); return False
    ev.write(f"    parses, {len(rs)} researcher(s)\n"); return True

_check("workflow-yaml-parses",
       "workflow.yaml parses and declares ≥1 researcher node",
       check_workflow_parses)


# --- 2. heterogeneity-3-families ----------------------------------------
def check_heterogeneity():
    wf = _load(os.path.join(RECIPE_DIR, "workflow.yaml"))
    fams = set()
    for n in (wf or {}).get("nodes") or []:
        if not isinstance(n, dict): continue
        lane = n.get("model_lane")
        if not lane: continue
        m = re.match(r"^([a-z]+)", str(lane))
        if m: fams.add(m.group(1))
    ev.write(f"    families = {sorted(fams)} (count={len(fams)})\n")
    return len(fams) >= 3

_check("heterogeneity-3-families",
       "≥3 distinct model_lane families across all nodes (HARD floor)",
       check_heterogeneity)


# --- 3. prompts-exist-nonempty ------------------------------------------
def check_prompts():
    wf = _load(os.path.join(RECIPE_DIR, "workflow.yaml"))
    missing, empty, checked = [], [], 0
    for n in (wf or {}).get("nodes") or []:
        if not isinstance(n, dict): continue
        ref = n.get("prompt_ref")
        if not ref: continue
        checked += 1
        p = os.path.join(RECIPE_DIR, ref)
        if not os.path.isfile(p): missing.append(ref)
        elif os.path.getsize(p) == 0: empty.append(ref)
    if missing: ev.write(f"    missing: {missing}\n"); return False
    if empty:   ev.write(f"    empty: {empty}\n"); return False
    ev.write(f"    {checked} prompt_refs all exist + non-empty\n"); return True

_check("prompts-exist-nonempty",
       "all referenced prompts/* files exist and are non-empty",
       check_prompts)


# --- 4. verifiers-executable-syntax -------------------------------------
def check_verifiers_syntax():
    wf = _load(os.path.join(RECIPE_DIR, "workflow.yaml"))
    refs = [n["verifier_ref"] for n in (wf or {}).get("nodes") or []
            if isinstance(n, dict) and n.get("verifier_ref")]
    vdir = os.path.join(RECIPE_DIR, "verifiers")
    if os.path.isdir(vdir):
        for e in sorted(os.listdir(vdir)):
            if e.endswith((".py", ".sh")):
                rel = os.path.join("verifiers", e)
                if rel not in refs:
                    refs.append(rel)
    all_ok = True
    for ref in refs:
        p = os.path.join(RECIPE_DIR, ref)
        if not os.path.isfile(p):
            ev.write(f"    missing: {ref}\n"); all_ok = False; continue
        mode = os.stat(p).st_mode
        if not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
            ev.write(f"    not +x: {ref}\n"); all_ok = False; continue
        if ref.endswith(".py"):
            cmd, label = ["python3", "-m", "py_compile", p], "py_compile"
        elif ref.endswith(".sh"):
            cmd, label = ["bash", "-n", p], "bash -n"
        else:
            ev.write(f"    unknown ext: {ref}\n"); all_ok = False; continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            ev.write(f"    {label} FAIL for {ref}:\n{r.stderr}\n")
            all_ok = False
        else:
            ev.write(f"    {ref}: {label} ok\n")
    return all_ok

_check("verifiers-executable-syntax",
       "all verifier files exist, are executable, and pass syntax check",
       check_verifiers_syntax)


# --- 5. contract-source-and-outputs -------------------------------------
EXPECTED_OUTPUT = "${MINI_ORK_RUN_DIR}/verifier-result-fail-probe.json"

def check_contract():
    p = os.path.join(RECIPE_DIR, "artifact_contract.yaml")
    if not os.path.isfile(p):
        ev.write(f"    missing: {p}\n"); return False
    c = _load(p)
    if not isinstance(c, dict):
        ev.write("    root not mapping\n"); return False
    if not c.get("source_artifact"):
        ev.write("    missing source_artifact\n"); return False
    outputs = c.get("outputs", [])
    if outputs != [EXPECTED_OUTPUT]:
        ev.write(f"    outputs = {outputs!r}, expected [{EXPECTED_OUTPUT!r}]\n")
        return False
    # Every output must start with ${MINI_ORK_RUN_DIR}/ so no path
    # resolves under the target working tree (publish-nothing-to-repo).
    for out in outputs:
        if not out.startswith("${MINI_ORK_RUN_DIR}/"):
            ev.write(f"    output {out!r} not under ${{MINI_ORK_RUN_DIR}}/\n")
            return False
    ev.write(f"    source={c['source_artifact']!r} outputs={outputs}\n")
    return True

_check("contract-source-and-outputs",
       f"contract declares source_artifact + outputs exactly "
       f"['{EXPECTED_OUTPUT}'] with no path under target working tree",
       check_contract)


# --- 6. task-class-keywords ---------------------------------------------
def check_task_class():
    p = os.path.join(RECIPE_DIR, "task_class.yaml")
    if not os.path.isfile(p):
        ev.write(f"    missing: {p}\n"); return False
    tc = _load(p)
    kw = ((tc or {}).get("matches") or {}).get("keywords", [])
    if not isinstance(kw, list) or len(kw) < 3:
        ev.write(f"    matches.keywords has {len(kw) if isinstance(kw, list) else 'non-list'}, need ≥3\n")
        return False
    ev.write(f"    matches.keywords has {len(kw)} entries: {kw}\n")
    return True

_check("task-class-keywords",
       "task_class.yaml declares matches.keywords with ≥3 entries",
       check_task_class)


# --- 7. rollback-keeps-artifacts ----------------------------------------
def check_rollback():
    wf = _load(os.path.join(RECIPE_DIR, "workflow.yaml"))
    rs = (wf or {}).get("rollback_strategy")
    if rs != "keep_artifacts_discard_publish":
        ev.write(f"    rollback_strategy = {rs!r}\n"); return False
    ev.write(f"    rollback_strategy = {rs!r}\n"); return True

_check("rollback-keeps-artifacts",
       "workflow.yaml declares rollback_strategy: keep_artifacts_discard_publish",
       check_rollback)


# --- Compose verdict ----------------------------------------------------
ev.close()
result = {"verifier": "structural-check",
          "pass": not reasons,
          "evidence_path": EVIDENCE,
          "sidecar_path": SIDECAR,
          "checks": checks,
          "reasons": reasons,
          "recipe_dir": RECIPE_DIR}
open(SIDECAR, "w", encoding="utf-8").write(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
sys.exit(0)  # always exit 0 — caller reads .pass from JSON
