#!/usr/bin/env python3
# Python port of human-decision.sh (bash-removal WS8). Identical logic (the .sh
# was a thin wrapper around an embedded Python program).

import json
import os
import sys
from pathlib import Path

run_dir = os.environ["MINI_ORK_RUN_DIR"]
path = Path(run_dir) / "human-decision.json"

errors = []
payload = {}
if not path.is_file() or path.stat().st_size == 0:
    errors.append("human-decision.json is required after the review packet is presented")
else:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"human-decision.json is not JSON: {exc.msg}")

if payload:
    decision = payload.get("decision")
    if decision not in {"approved", "revise"}:
        errors.append("decision must be approved or revise")
    if not isinstance(payload.get("approver"), str) or not payload["approver"].strip():
        errors.append("approver is required")
    if decision == "revise" and not isinstance(payload.get("feedback_delta"), str):
        errors.append("feedback_delta is required when decision is revise")

approved = payload.get("decision") == "approved" and not errors
result = {
    "pass": approved,
    "status": "approved" if approved else ("revision_requested" if not errors else "invalid"),
    "errors": errors,
}
print(json.dumps(result))
sys.exit(0 if approved else 1)
