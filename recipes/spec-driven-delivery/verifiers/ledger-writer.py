#!/usr/bin/env python3
"""ledger-writer — traceability ledger and slide-back gauges.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (ledger_writer; rule 6).
Appends rows to ${MINI_ORK_RUN_DIR}/ledger.jsonl:
{ts, spec_id, clause_id, deliverable_id, child_run_id, commit, gate_id,
verdict, waived}, including WAIVED gates and human veto events. Computes
slide-back gauges {waived_gates, failed_gates, asks_open} (plus the
spec-edit vs code-edit ratio) into ${MINI_ORK_RUN_DIR}/slide-back.json.
Fails only on missing or corrupt inputs.

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
