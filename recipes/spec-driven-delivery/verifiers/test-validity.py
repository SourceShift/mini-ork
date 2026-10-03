#!/usr/bin/env python3
"""test-validity — broken-baseline check on test_author's gates.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (test_validity; rule 3,
test-first). For every probe in ${MINI_ORK_RUN_DIR}/gates/<spec_id>.json:
each kind=cmd probe runs on the CURRENT, untouched tree with timeout
MO_SDD_SMOKE_TIMEOUT_S (default 120). A probe tagged `precondition` must pass
now; any other probe that passes now is vacuous and fails the gate. Static
vacuity checks for all kinds: probe non-empty and not `true`/`exit 0`/`:`,
`expect` non-empty. Spec coverage: every SpecCard acceptance id has exactly
one probe. Results go to ${MINI_ORK_RUN_DIR}/test-validity.json.

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
