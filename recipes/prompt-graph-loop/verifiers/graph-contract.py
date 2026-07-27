#!/usr/bin/env python3
# Python port of graph-contract.sh (bash-removal WS8). Identical logic (the .sh
# was a thin wrapper around an embedded Python program).

import json
import os
import sys
from pathlib import Path

run_dir = os.environ["MINI_ORK_RUN_DIR"]
graph_path = Path(run_dir) / "agent-graph.json"
draft_path = Path(run_dir) / "draft-artifact.md"
verification_path = Path(run_dir) / "verification-report.json"
report_path = Path(run_dir) / "graph-contract-report.json"

errors = []
graph = {}
verification = {}

for path, label in ((graph_path, "agent graph"), (draft_path, "draft artifact"),
                    (verification_path, "verification report")):
    if not path.is_file() or path.stat().st_size == 0:
        errors.append(f"missing or empty {label}: {path.name}")

if not errors:
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"agent graph is not JSON: {exc.msg}")
    try:
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"verification report is not JSON: {exc.msg}")

if graph:
    if not isinstance(graph.get("nodes"), list) or not graph["nodes"]:
        errors.append("agent graph requires a non-empty nodes array")
    if not isinstance(graph.get("edges"), list):
        errors.append("agent graph requires an edges array")
    if not isinstance(graph.get("artifacts"), list):
        errors.append("agent graph requires an artifacts array")

if verification:
    if verification.get("verdict") not in {"pass", "revise"}:
        errors.append("verification report verdict must be pass or revise")
    for key in ("claims_checked", "graph_completeness", "output_contract", "findings", "next_action"):
        if key not in verification:
            errors.append(f"verification report missing {key}")

payload = {"pass": not errors, "errors": errors}
report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload))
sys.exit(0 if payload["pass"] else 1)
