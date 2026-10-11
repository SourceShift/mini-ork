#!/usr/bin/env python3
# Verifier for orch-smoke: DETERMINISTICALLY FAILS.
#
# Purpose: prove the per-run orchestrator engages by default. The node fails
# with rc=1 and no retries edge, so at the wave boundary the orchestrator
# classifies the outcome and emits exactly one ``orchestrator.<kind>`` row
# into run_events — with MO_RUN_ORCHESTRATOR deliberately UNSET. Zero LLM
# cost: this script is the only thing that runs.
import json
import os
import sys

RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR", ".")

result = json.dumps({
    "verifier": "fail-probe",
    "pass": False,
    "reasons": ["orch-smoke: this verifier fails on purpose — the run exists "
                "to make a node fail and prove the orchestrator fires default-on"],
    "checks": {"deterministic_failure": False},
})
print(result)
try:
    with open(os.path.join(RUN_DIR, "verifier-result-fail-probe.json"), "w") as f:
        f.write(result + "\n")
except OSError:
    pass
sys.exit(1)
