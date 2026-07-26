#!/usr/bin/env bash
set -Eeuo pipefail

run_dir="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR is required}"
graph="$run_dir/agent-graph.json"
draft="$run_dir/draft-artifact.md"
verification="$run_dir/verification-report.json"
report="$run_dir/graph-contract-report.json"

python3 - "$graph" "$draft" "$verification" "$report" <<'PY'
import json
import sys
from pathlib import Path

graph_path, draft_path, verification_path, report_path = map(Path, sys.argv[1:])
errors = []
graph = {}
verification = {}

for path, label in ((graph_path, "agent graph"), (draft_path, "draft artifact"), (verification_path, "verification report")):
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
raise SystemExit(0 if payload["pass"] else 1)
PY
