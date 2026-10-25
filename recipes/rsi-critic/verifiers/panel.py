#!/usr/bin/env python3
"""Grade the critic panel from what `llm_calls` says actually served.

Two failure modes this catches that a verdict check cannot: both critics landing
on one family after a fallback (then they are one opinion, twice), and a critic
that was served by a lane the config did not intend.

**Only the declared critics are graded.** Every actor that logs a call includes
the engine's own ``planner`` and ``profile_answerer`` lanes; grading those would
report ``same_family`` against a critic that legitimately shares their provider
and quarantine every cycle, which is the defect the first live cycle hit in the
sibling recipe. The panel is exactly the ``critic_*`` nodes in
``workflow.yaml``.

Fail-open when there is nothing to grade — no DB, no table, no rows — matching
`check_panel`'s contract. An unmeasured panel is reported as skipped, never as
healthy; G10 still grades the families recorded on the verdicts themselves.
Fail closed when the workflow declares no critic at all.
"""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import brief_path, read_json, write_json

STAGE = "panel"
PANEL_PREFIX = "critic_"
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

    brief = read_json(brief_path(), STAGE)
    implementer = str(brief.get("implementer_lane") or "").strip()
    if implementer and result.get("families"):
        from mini_ork.verify.panel_heterogeneity import lane_family

        implementer_family = lane_family(implementer)
        for alias, families in result["families"].items():
            if implementer_family in families:
                result.setdefault("problems", []).append(
                    f"implementer_family:{alias}:{implementer_family}")
        result["ok"] = not result["problems"]

    write_json("panel-composition.json", result, "rsi-critic-panel")
    if not result.get("ok", True):
        sys.stderr.write(f"{STAGE}: panel problems: {', '.join(result.get('problems') or [])}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
