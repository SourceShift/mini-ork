#!/usr/bin/env python3
"""specdir-ingest — pipeline step 1: deterministic spec-directory ingestion.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (specdir_ingest).
Reads the spec dir from the kickoff's `## Spec dir:` line (MINI_ORK_KICKOFF
or the run's kickoff copy) and shells
`bin/mini-ork specs ingest <spec_dir> --out ${MINI_ORK_RUN_DIR}/spec-index.json`.
MO_SDD_SPEC_GLOB (default *.md) selects spec files. Pass iff the command
exits 0, spec-index.json validates against schemas/spec-index.schema.json,
and it lists at least MO_SDD_MIN_SPECS (default 1) specs.

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
