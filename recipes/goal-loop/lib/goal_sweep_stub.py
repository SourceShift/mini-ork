#!/usr/bin/env python3
"""Stub dispatcher for the goal-loop `sweep_dispatcher` implementer submode.

In U4a this recipe only PLANS — actual child spawning arrives with the U4b
driver. The stub is registered via ``register_implementer_submode`` so the
recipe-local loader is dogfooded end-to-end; the runtime reads the (recipe,
node_id) entry, invokes this script as a subprocess, and consumes whatever
JSON it prints to stdout. The script emits a single line that the dispatch
layer treats as a planned-only acknowledgment.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    json.dump({"status": "planned_only", "reason": "U4a skeleton — U4b drives"}, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())