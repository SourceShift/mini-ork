#!/usr/bin/env python3
# Grades the procedure recipe's step report: every step line is checked.
#
#   procedure-report.md must exist under MINI_ORK_RUN_DIR, carry a "## Steps"
#   section, and list at least one "- [x] N." line and no "- [ ] N." line.
#
# rc IS the verdict (0 = every step done, 1 = missing, empty or unfinished);
# the JSON on stdout explains it.
from __future__ import annotations

import json
import os
import re
import sys

STEP = re.compile(r"^- \[( |x|X)\] (\d+)\.", re.M)


def grade(report_path: str) -> dict:
    if not os.path.isfile(report_path):
        return {"ok": False, "reason": "procedure-report.md missing", "done": [], "open": []}
    text = open(report_path, encoding="utf-8").read()
    match = re.search(r"^## Steps\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        return {"ok": False, "reason": "no '## Steps' section", "done": [], "open": []}
    done = [int(n) for mark, n in STEP.findall(match.group(1)) if mark.lower() == "x"]
    open_steps = [int(n) for mark, n in STEP.findall(match.group(1)) if mark == " "]
    if not done and not open_steps:
        return {"ok": False, "reason": "no step lines", "done": [], "open": []}
    reason = f"unfinished steps: {open_steps}" if open_steps else ""
    return {"ok": not open_steps, "reason": reason, "done": done, "open": open_steps}


def main() -> int:
    result = grade(os.path.join(os.environ["MINI_ORK_RUN_DIR"], "procedure-report.md"))
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
