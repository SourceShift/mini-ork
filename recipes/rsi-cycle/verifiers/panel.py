#!/usr/bin/env python3
"""Grade the lenses that actually served, from `llm_calls`.

The workflow declares three lenses on three families. This node reports what
answered: a lane can fall back to another provider mid-run, and a panel that
quietly became one family is exactly the failure the multi-family layout exists
to avoid.

**Only the declared panel members are graded.** The first live cycle
(2026-10-25) graded every actor that logged a call and reported
``same_family:codex_lens,deepseek_lens`` — the synthesiser is deliberately in
the implementer's family — plus collisions with the engine's own ``planner``
and ``profile_answerer`` lanes. That demanded a fourth distinct family no stock
home can provide, so a correct recipe could never pass. The panel is exactly the
``lens_*`` nodes in ``workflow.yaml``; reading the declaration keeps the two in
one place.

Fail-open when there is nothing to grade (no DB, no table, no rows), matching
`check_panel`'s own contract: an unmeasured panel is reported as skipped, never
as healthy. Fail closed when the workflow declares no lens at all — that is a
recipe bug, and skipping it would let the bug through.
"""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import write_json

STAGE = "panel"
PANEL_PREFIX = "lens_"
WORKFLOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workflow.yaml")


def _declared_lanes() -> list[str]:
    """The ``model_lane`` of every node the workflow names as a panel member."""
    with open(WORKFLOW, encoding="utf-8") as handle:
        workflow = yaml.safe_load(handle) or {}
    lanes: list[str] = []
    for node in workflow.get("nodes") or []:
        if isinstance(node, dict) and str(node.get("name", "")).startswith(PANEL_PREFIX):
            lane = str(node.get("model_lane") or "").strip()
            if lane:
                lanes.append(lane)
    return lanes


def main() -> None:
    run_id = os.environ.get("MINI_ORK_RUN_ID", "").strip()
    db = os.environ.get("MINI_ORK_DB", "").strip()
    try:
        lanes = _declared_lanes()
    except OSError as exc:
        sys.stderr.write(f"{STAGE}: cannot read {WORKFLOW}: {exc}\n")
        raise SystemExit(1) from exc
    if not lanes:
        sys.stderr.write(f"{STAGE}: workflow.yaml declares no {PANEL_PREFIX}* node\n")
        raise SystemExit(1)
    result = {"ok": True, "problems": [], "skipped": None, "served": {}, "families": {},
              "lanes": lanes}
    if not run_id:
        result["skipped"] = "MINI_ORK_RUN_ID is not set"
    else:
        from mini_ork.verify.panel_heterogeneity import check_panel

        result.update(check_panel(run_id, lanes, db=db or None))

    write_json("panel-composition.json", result, "rsi-cycle-panel")
    if not result.get("ok", True):
        sys.stderr.write(f"{STAGE}: panel problems: {', '.join(result.get('problems') or [])}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
