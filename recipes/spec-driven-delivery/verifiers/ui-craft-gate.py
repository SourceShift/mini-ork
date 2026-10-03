#!/usr/bin/env python3
"""ui-craft-gate — conditional UI/UX craft judge.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (ui_craft_gate).
Applies only to SpecCards with ui_craft.required = true. Runs the
MO_SDD_UI_GATE_CMD template (vars {spec_id}, {design_sources}, {run_dir});
a nonzero exit fails. An empty MO_SDD_UI_GATE_CMD means the gate is skipped
with a WAIVED verdict (pass true, "waived": true) that ledger_writer records
as a WAIVED ledger row, counted by the slide-back gauge.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

Stub until K3 (kickoffs/sdd/k3-verifiers.md): prints a not-implemented
verdict and exits 2, so a live run fails closed at this gate.
"""
import json
import sys


def main():
    print(json.dumps({"pass": False, "reason": "NOT_IMPLEMENTED"}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
