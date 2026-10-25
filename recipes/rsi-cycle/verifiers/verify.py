#!/usr/bin/env python3
"""The run's success verifier: the artifacts the cycle reads must exist and agree.

Deliberately narrow. Policy (frozen paths, prefixes, registration, already-fixed)
belongs to `mini_ork.rsi.select`, which sees the whole ledger; repeating it here
would give two answers to one question. This node only says: the proposal is
present, it is shaped the way `Proposal.from_mapping` expects, and the panel
report did not record a problem.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import RUN_DIR, fail, read_json


def main() -> None:
    proposal = read_json(os.path.join(RUN_DIR, "proposal.json"), "verify")
    if not str(proposal.get("candidate_id") or "").strip():
        fail("verify", "proposal.json has no candidate_id")
    if not proposal.get("files"):
        fail("verify", "proposal.json names no files")
    if not str(proposal.get("verification_command") or "").strip():
        fail("verify", "proposal.json has no verification_command")

    panel = read_json(os.path.join(RUN_DIR, "panel-composition.json"), "verify")
    if panel.get("problems"):
        fail("verify", f"the panel recorded problems: {panel['problems']}")

    read_json(os.path.join(RUN_DIR, "lens-panel.json"), "verify")
    print(json.dumps({"verifier": "rsi-cycle-verify", "pass": True,
                      "skipped_panel": bool(panel.get("skipped"))}))


if __name__ == "__main__":
    main()
