#!/usr/bin/env python3
"""The run's success verifier: two well-formed verdicts exist.

It does NOT require approval and does NOT require unanimity. Those are G10's
decisions, made from `critics.json` against the ledger and the implementer's
family; a verifier that pre-approved would let the recipe bypass the gate it
feeds. A run that produces two honest "no"s is a successful run — the cycle
quarantines, which is the correct outcome.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import RUN_DIR, fail, read_json

MIN_CRITICS = 2


def main() -> None:
    critics = read_json(os.path.join(RUN_DIR, "critics.json"), "verify")
    verdicts = critics.get("critics") or []
    if len(verdicts) < MIN_CRITICS:
        fail("verify", f"{len(verdicts)} verdict(s) merged; {MIN_CRITICS} independent "
                       f"critics are required before a decision can be made")
    for verdict in verdicts:
        if not isinstance(verdict.get("approve"), bool):
            fail("verify", f"critic {verdict.get('alias', '?')} has no boolean 'approve'")

    panel = read_json(os.path.join(RUN_DIR, "panel-composition.json"), "verify")
    print(json.dumps({"verifier": "rsi-critic-verify", "pass": True,
                      "approvals": sum(1 for v in verdicts if v["approve"]),
                      "skipped_panel": bool(panel.get("skipped"))}))


if __name__ == "__main__":
    main()
