#!/usr/bin/env python3
# verifiers/recipe-validator.py — single combined validator for the
# meta-recipe-creator. Enforces:
#   1. STRUCTURE  — all required files exist + chmod-able / parseable
#   2. HETEROGENEITY (HARD per user spec) — ≥3 distinct model_lane families
#                    across the chosen recipe's workflow.yaml
#   3. DRY-RUN    — workflow.yaml parses; every prompt_ref / verifier_ref
#                   resolves; every node_type is from the strict 8-element
#                   enum; every edge endpoint exists
#
# Python port of recipe-validator.sh (bash-removal WS8). Same checks, evidence
# text, JSON schema, and rc semantics.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR   run directory (set by the native execute runtime)
#   MINI_ORK_ROOT      repo root (set by mini-ork wrappers)
#
# Output: single JSON object on stdout
#   { "verifier": "recipe-validator", "pass": bool, "evidence_path": "...",
#     "structure": {...}, "heterogeneity": {...}, "dry_run": {...},
#     "checks_run": [...], "failed_checks": [...] }
# Exit code: always 0 (caller reads .pass from JSON).

import json
import os
import re
import subprocess
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
MINI_ORK_ROOT = os.environ.get("MINI_ORK_ROOT") or \
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

EVIDENCE = os.path.join(RUN_DIR, "verifier-recipe-validator.log")
ev = open(EVIDENCE, "w")

# Resolve the chosen draft path.
RECIPE_NAME_FILE = os.path.join(RUN_DIR, "chosen", "recipe_name")
if not os.path.isfile(RECIPE_NAME_FILE):
    ev.write("FAIL: chosen/recipe_name missing — arbiter never wrote it\n")
    ev.close()
    print(json.dumps({"verifier": "recipe-validator", "pass": False,
                      "evidence_path": EVIDENCE,
                      "failed_checks": ["chosen-recipe-name-missing"]}))
    sys.exit(0)
RECIPE_NAME = re.sub(r"\s", "", open(RECIPE_NAME_FILE, encoding="utf-8").read())
CHOSEN_DIR = os.path.join(RUN_DIR, "chosen", RECIPE_NAME)
if not os.path.isdir(CHOSEN_DIR):
    ev.write(f"FAIL: chosen/{RECIPE_NAME}/ dir missing\n")
    ev.close()
    print(json.dumps({"verifier": "recipe-validator", "pass": False,
                      "evidence_path": EVIDENCE,
                      "failed_checks": ["chosen-dir-missing"]}))
    sys.exit(0)

ev.write(f"── recipe-validator: chosen={RECIPE_NAME} path={CHOSEN_DIR} ──\n")
ev.flush()

checks_run = []
failed_checks = []


def _check(cid, desc, fn):
    checks_run.append(cid)
    ev.write(f"  [{cid}] {desc}\n")
    ev.flush()
    try:
        ok = bool(fn())
    except Exception as exc:
        ev.write(f"{type(exc).__name__}: {exc}\n")
        ok = False
    if ok:
        ev.write("    ok\n")
    else:
        ev.write("    FAIL\n")
        failed_checks.append(cid)
    ev.flush()
    return ok


# ── 1. STRUCTURE ──────────────────────────────────────────────────────
_check("name-kebab", "recipe_name is kebab-case ≤ 32 chars",
       lambda: re.match(r"^[a-z][a-z0-9-]{1,31}$", RECIPE_NAME) is not None
       and not RECIPE_NAME.endswith("-"))
_check("workflow-exists", "workflow.yaml exists",
       lambda: os.path.isfile(os.path.join(CHOSEN_DIR, "workflow.yaml")))
_check("task-class-exists", "task_class.yaml exists",
       lambda: os.path.isfile(os.path.join(CHOSEN_DIR, "task_class.yaml")))
_check("contract-exists", "artifact_contract.yaml exists",
       lambda: os.path.isfile(os.path.join(CHOSEN_DIR, "artifact_contract.yaml")))


def _prompts_dir():
    d = os.path.join(CHOSEN_DIR, "prompts")
    return os.path.isdir(d) and len([f for f in os.listdir(d) if f.endswith(".md")]) >= 1


_check("prompts-dir", "prompts/ dir exists with ≥1 .md", _prompts_dir)


def _verifiers_dir():
    # .py world: generated verifiers may be .py (preferred) or .sh (deprecated).
    d = os.path.join(CHOSEN_DIR, "verifiers")
    return os.path.isdir(d) and \
        len([f for f in os.listdir(d) if f.endswith((".sh", ".py"))]) >= 1


_check("verifiers-dir", "verifiers/ dir exists with ≥1 .sh", _verifiers_dir)
_check("example-kickoff", "example-kickoff.md exists + non-empty",
       lambda: os.path.isfile(os.path.join(CHOSEN_DIR, "example-kickoff.md"))
       and os.path.getsize(os.path.join(CHOSEN_DIR, "example-kickoff.md")) > 0)
_check("readme", "README.md exists + non-empty",
       lambda: os.path.isfile(os.path.join(CHOSEN_DIR, "README.md"))
       and os.path.getsize(os.path.join(CHOSEN_DIR, "README.md")) > 0)

# ── 2. DRY-RUN — YAML parses, refs resolve, node_type enum holds ──
DRY_RUN_JSON = os.path.join(RUN_DIR, "recipe-validator-dry-run.json")

dry = {"yaml_parses": False, "node_types_valid": False, "refs_resolve": False,
       "edges_endpoints_exist": False, "details": []}
try:
    import yaml
except ImportError:
    ev.write("FAIL: PyYAML not installed\n")
    dry["details"].append("PyYAML missing")
    json.dump(dry, open(DRY_RUN_JSON, "w"))
    yaml = None

if yaml is not None:
    VALID_TYPES = {"planner", "researcher", "implementer", "reviewer",
                   "verifier", "reflector", "publisher", "rollback"}
    try:
        wf = yaml.safe_load(open(os.path.join(CHOSEN_DIR, "workflow.yaml"), encoding="utf-8"))
        dry["yaml_parses"] = True
    except Exception as e:
        dry["details"].append(f"workflow.yaml parse: {e}")
        wf = None
    if wf is not None:
        nodes = wf.get("nodes") or []
        edges = wf.get("edges") or []
        node_names = {n["name"] for n in nodes}

        # node_type enum
        bad_types = [(n["name"], n.get("type")) for n in nodes if n.get("type") not in VALID_TYPES]
        dry["node_types_valid"] = not bad_types
        if bad_types:
            dry["details"].append(f"invalid node_types: {bad_types}")

        # prompt_ref / verifier_ref resolve
        missing = []
        for n in nodes:
            p = n.get("prompt_ref")
            v = n.get("verifier_ref")
            if p and p != "null" and not os.path.exists(os.path.join(CHOSEN_DIR, p)):
                missing.append(f"{n['name']}.prompt_ref={p}")
            if v and v != "null" and not os.path.exists(os.path.join(CHOSEN_DIR, v)):
                missing.append(f"{n['name']}.verifier_ref={v}")
        dry["refs_resolve"] = not missing
        if missing:
            dry["details"].append(f"missing refs: {missing}")

        # edges endpoints exist
        bad_edges = [e for e in edges
                     if e.get("from") not in node_names or e.get("to") not in node_names]
        dry["edges_endpoints_exist"] = not bad_edges
        if bad_edges:
            dry["details"].append(f"orphan edges: {bad_edges}")

    json.dump(dry, open(DRY_RUN_JSON, "w"))
    ev.write(f"  yaml_parses={dry['yaml_parses']} node_types_valid={dry['node_types_valid']} "
             f"refs_resolve={dry['refs_resolve']} edges_ok={dry['edges_endpoints_exist']}\n")
    ev.flush()


def _dry_flag(key):
    return os.path.isfile(DRY_RUN_JSON) and bool(json.load(open(DRY_RUN_JSON)).get(key))


_check("dry-run-yaml", "workflow.yaml parses as YAML", lambda: _dry_flag("yaml_parses"))
_check("dry-run-node-types", "all node_types in strict 8-element enum",
       lambda: _dry_flag("node_types_valid"))
_check("dry-run-refs", "every prompt_ref / verifier_ref resolves", lambda: _dry_flag("refs_resolve"))
_check("dry-run-edges", "every edge.from / edge.to is a declared node",
       lambda: _dry_flag("edges_endpoints_exist"))


# All verifier stubs must syntax-check (bash -n for .sh, py_compile for .py).
def _verifiers_syntax():
    ok = True
    d = os.path.join(CHOSEN_DIR, "verifiers")
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith((".sh", ".py")):
            continue
        vf = os.path.join(d, f)
        if f.endswith(".py"):
            rc = subprocess.run([sys.executable, "-m", "py_compile", vf],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        else:
            with open(EVIDENCE, "ab") as evf:
                rc = subprocess.run(["bash", "-n", vf],
                                    stdout=subprocess.DEVNULL, stderr=evf).returncode
        if rc != 0:
            ok = False
            ev.write(f"    syntax FAIL: {vf}\n")
            ev.flush()
    return ok


SYNTAX_OK = _verifiers_syntax()
_check("verifiers-bash-syntax", "every verifiers/*.sh passes bash -n", lambda: SYNTAX_OK)

# ── 3. HETEROGENEITY (HARD — ≥3 distinct families) ────────────────────
HETERO_JSON = os.path.join(RUN_DIR, "recipe-validator-heterogeneity.json")

het = {"families": {}, "distinct_count": 0, "lanes_used": [], "unresolved_lanes": []}
if yaml is None:
    het["unresolved_lanes"].append("PyYAML missing")
    json.dump(het, open(HETERO_JSON, "w"))
else:
    # Load chosen recipe's workflow + the live agents.yaml lane → family map.
    try:
        wf = yaml.safe_load(open(os.path.join(CHOSEN_DIR, "workflow.yaml"), encoding="utf-8")) or {}
    except Exception as e:
        het["unresolved_lanes"].append(f"workflow.yaml parse: {e}")
        wf = None

    if wf is None:
        json.dump(het, open(HETERO_JSON, "w"))
    else:
        # Find agents.yaml — try .mini-ork/config first, fall back to repo-root config.
        agents_paths = [
            os.path.join(os.environ.get("MINI_ORK_HOME", ""), "config", "agents.yaml"),
            os.path.join(MINI_ORK_ROOT, ".mini-ork", "config", "agents.yaml"),
            os.path.join(MINI_ORK_ROOT, "config", "agents.yaml"),
        ]
        lane_to_family = {}
        for p in agents_paths:
            if p and os.path.exists(p):
                try:
                    a = yaml.safe_load(open(p, encoding="utf-8")) or {}
                    lane_to_family = a.get("lanes") or {}
                    break
                except Exception:
                    continue

        # Heuristic: lane name itself often encodes family (e.g. glm_lens → glm).
        # Fall back to that when agents.yaml lookup is empty.
        def family_of(lane):
            if lane in lane_to_family:
                return lane_to_family[lane]
            m = re.match(r"^([a-z]+)(?:_lens|_drafter)?$", lane or "")
            return m.group(1) if m else lane

        lanes_used = []
        for n in (wf.get("nodes") or []):
            ln = n.get("model_lane")
            if ln:
                lanes_used.append(ln)
        het["lanes_used"] = lanes_used

        families = {}
        unresolved = []
        for ln in lanes_used:
            fam = family_of(ln)
            if fam is None:
                unresolved.append(ln)
                continue
            families[fam] = families.get(fam, 0) + 1
        het["families"] = families
        het["distinct_count"] = len(families)
        het["unresolved_lanes"] = unresolved
        json.dump(het, open(HETERO_JSON, "w"))
        ev.write(f"  distinct_families={het['distinct_count']} families={list(families.keys())}\n")
        ev.flush()

_check("heterogeneity-3-families", "≥ 3 distinct model_lane families (HARD floor)",
       lambda: os.path.isfile(HETERO_JSON)
       and json.load(open(HETERO_JSON)).get("distinct_count", 0) >= 3)

# ── Compose verdict ──────────────────────────────────────────────────
passed = not failed_checks

dry_out = json.load(open(DRY_RUN_JSON)) if os.path.exists(DRY_RUN_JSON) else {}
het_out = json.load(open(HETERO_JSON)) if os.path.exists(HETERO_JSON) else {}
out = {
    "verifier": "recipe-validator",
    "pass": passed,
    "evidence_path": EVIDENCE,
    "checks_run": checks_run,
    "failed_checks": failed_checks,
    "dry_run": dry_out,
    "heterogeneity": het_out,
}
print(json.dumps(out))

ev.close()
sys.exit(0)
