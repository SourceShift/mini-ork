#!/usr/bin/env python3
"""Collect the critic verdicts into the one file the cycle reads.

No voting. Every verdict is carried through verbatim; unanimity and family
diversity are `mini_ork.rsi.gates` G10's call, made against the same file. A
merge step that decided anything would be a second judge with a quieter process.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import as_list, fail, input_many, read_json, write_json

STAGE = "merge"


def main() -> None:
    paths = input_many("verdicts", ".json")
    if not paths:
        fail(STAGE, "no critic verdicts arrived")

    critics = []
    for path in paths:
        lane = os.path.splitext(os.path.basename(path))[0]
        verdict = read_json(path, STAGE)
        approve = verdict.get("approve")
        # A string would coerce truthily: `"approve": "no"` must never become a
        # yes. Only a JSON boolean is an answer; anything else is an abstention,
        # and an abstention must block.
        if not isinstance(approve, bool):
            fail(STAGE, f"{lane} returned approve={approve!r}; a verdict must answer "
                        f"with a JSON boolean, and an abstention must block")
        critics.append({
            "alias": lane,
            "lane": lane,
            "approve": approve,
            "blocking_findings": as_list(verdict.get("blocking_findings")),
            "confidence": verdict.get("confidence"),
            "why": str(verdict.get("why") or ""),
        })

    write_json("critics.json", {"critics": critics, "count": len(critics)},
               "rsi-critic-merge")


if __name__ == "__main__":
    main()
