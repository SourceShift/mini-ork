"""Deterministic recipe fallback plan + artifact-contract overlay (pure transforms).

Extracted verbatim from ``mini_ork.cli.plan`` (strangler-fig parity port).
Both functions read recipe YAML/JSON files and return plan JSON strings; they
perform no LLM dispatch, DB access, or CLI stream writes. ``mini_ork.cli.plan``
re-exports both names.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def recipe_fallback_plan(recipe, workflow_path, root, kickoff) -> str | None:
    if not recipe or not workflow_path or not os.path.isfile(workflow_path):
        return None
    import yaml
    workflow = yaml.safe_load(Path(workflow_path).read_text(encoding="utf-8")) or {}
    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    contract_path = Path(root) / "recipes" / recipe / "artifact_contract.yaml"
    contract = {}
    if contract_path.exists():
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    outputs = contract.get("outputs") or workflow.get("outputs") or []
    success_verifiers = contract.get("success_verifiers") or workflow.get("success_verifiers") or []
    plan = {
        "objective": f"Execute recipe {recipe} for {kickoff}",
        "assumptions": [
            "Recipe workflow.yaml is the source of truth for dispatch order.",
            "Planner LLM output was invalid JSON, so mini-ork generated this deterministic recipe plan.",
        ],
        "decomposition": [
            {"id": n.get("name"),
             "description": n.get("description") or f"{n.get('type', 'unknown')} node {n.get('name')}",
             "node_type": n.get("type"),
             "depends_on": [e.get("from") for e in edges if e.get("to") == n.get("name") and e.get("from")]}
            for n in nodes if isinstance(n, dict) and n.get("name")],
        "dependencies": [{"from": e.get("from"), "to": e.get("to")}
                         for e in edges if isinstance(e, dict) and e.get("from") and e.get("to")],
        "risk_notes": [
            "Fallback plan does not include model-authored verifier shell commands.",
            "Execution still uses the recipe workflow nodes and recipe verifier_ref scripts.",
        ],
        "artifact_contract": {"outputs": outputs, "success_verifiers": success_verifiers},
        "verifier_contract": {"checks": [
            {"id": "recipe-workflow-dispatch",
             "description": f"Dispatch every node declared in recipes/{recipe}/workflow.yaml."},
            {"id": "recipe-artifacts",
             "description": "Recipe artifact contract is satisfied by execute/verify."}]},
    }
    return json.dumps(plan, indent=2)


def overlay_plan(plan_json, task_class, profile_path, root) -> str:
    try:
        p = json.loads(plan_json)
    except Exception:
        return plan_json
    p.setdefault("task_class", task_class)
    recipe = ""
    if profile_path and os.path.isfile(profile_path):
        try:
            recipe = (json.load(open(profile_path)).get("recipe") or "").strip()
        except Exception:
            recipe = ""
    contract_yaml = os.path.join(root, "recipes", recipe, "artifact_contract.yaml") if recipe else ""
    if contract_yaml and os.path.isfile(contract_yaml):
        try:
            import yaml
            recipe_contract = yaml.safe_load(open(contract_yaml)) or {}
            recipe_verifiers = recipe_contract.get("success_verifiers") or []
            if recipe_verifiers:
                ac = p.get("artifact_contract")
                if not isinstance(ac, dict):
                    ac = {}
                prose = ac.get("success_verifiers") or []
                if prose and prose != recipe_verifiers:
                    ac.setdefault("acceptance_criteria", prose)
                ac["success_verifiers"] = recipe_verifiers
                if recipe_contract.get("outputs"):
                    ac.setdefault("outputs", recipe_contract["outputs"])
                p["artifact_contract"] = ac
        except Exception:
            pass
    return json.dumps(p, indent=2)
