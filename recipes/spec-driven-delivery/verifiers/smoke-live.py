#!/usr/bin/env python3
"""smoke-live — execute every acceptance gate against the live surface.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (smoke_live; rule 4,
gates execute behavior). kind=cmd and kind=contract probes run as
subprocesses with the MO_SDD_SURFACE_ENV dotenv loaded (base URLs, tokens)
and a MO_SDD_SMOKE_TIMEOUT_S timeout (default 120; a hung probe is a FAIL
with reason timeout). A gate passes iff exit 0 and the output satisfies
`expect`. kind=ui probes go through the MO_SDD_UI_PROBE_CMD template when it
is set, else the gate is WAIVED (recorded, not passed). Verdicts are atomic
per gate with no partial credit: the node passes iff zero gates FAILED, and
the WAIVED count is recorded. Results go to ${MINI_ORK_RUN_DIR}/smoke-live.json.

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
