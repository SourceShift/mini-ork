#!/usr/bin/env python3
"""ratification-check — every source requirement is covered or flagged.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (ratification_check).
For each ${MINI_ORK_RUN_DIR}/spec-cards/<spec_id>.json: validates against
schemas/spec-card.schema.json; source_hash still matches the source file
bytes (spec drift = fail); every acceptance entry has exactly one gate; every
deliverable's acceptance_refs resolve and every acceptance id is referenced by
a deliverable. Reads ${MINI_ORK_RUN_DIR}/ratification/<spec_id>.json (kept
out of the card, whose schema allows no extra keys): fail unless its
`ratification` list is empty or every entry has `acknowledged: true`.

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
