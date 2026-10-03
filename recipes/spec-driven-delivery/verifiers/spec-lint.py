#!/usr/bin/env python3
"""spec-lint — authoring-error lint over the indexed specs.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (spec_lint: 7 authoring
errors, 4-property test). Runs mini_ork.specdir.lint over every spec in
${MINI_ORK_RUN_DIR}/spec-index.json and writes the findings
({spec_id, code, severity, message}) to ${MINI_ORK_RUN_DIR}/spec-lint.json.
Fails on any error-severity finding (NO_ACCEPTANCE, DUP_ID, DEP_CYCLE);
warnings are reported but do not fail.

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
