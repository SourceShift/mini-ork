#!/usr/bin/env python3
"""dispatch-aggregator — recompute per-deliverable verdicts from child runs.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (dispatch_aggregator;
rule 2, the verifier payload is the only verdict). Reads
${MINI_ORK_RUN_DIR}/dispatch-results.json and writes
${MINI_ORK_RUN_DIR}/aggregate-verdict.json with shape
{total, delivered, failed, blocked, pending, pass_rate, deliverables[]}.
Never trusts the dispatcher's claims: every non-blocked deliverable needs a
child_run_id whose child_run_dir exists and holds a non-empty verdict
artifact, and its status is recomputed from that artifact. Every blocked
deliverable needs an ASK at its recorded ask_path,
${MINI_ORK_RUN_DIR}/asks/<spec_id>--<deliverable_id>.json. Pass iff every
deliverable is delivered.

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
