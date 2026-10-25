#!/usr/bin/env python3
"""Grade the critic panel from what `llm_calls` says actually served.

Two failure modes this catches that a verdict check cannot: both critics landing
on one family after a fallback (then they are one opinion, twice), and a critic
that was served by a lane the config did not intend.

Fail-open when there is nothing to grade — no DB, no table, no rows — matching
`check_panel`'s contract. An unmeasured panel is reported as skipped, never as
healthy; G10 still grades the families recorded on the verdicts themselves.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import brief_path, read_json, served_actors, write_json

STAGE = "panel"


def main() -> None:
    run_id = os.environ.get("MINI_ORK_RUN_ID", "").strip()
    db = os.environ.get("MINI_ORK_DB", "").strip()
    result = {"ok": True, "problems": [], "skipped": None, "served": {}, "families": {}}
    actors = served_actors(db, run_id) if (db and run_id and os.path.isfile(db)) else None
    if actors is None:
        result["skipped"] = "no usable llm_calls table"
    elif not actors:
        result["skipped"] = f"no llm_calls rows for run {run_id!r}"
    else:
        from mini_ork.verify.panel_heterogeneity import check_panel

        result = check_panel(run_id, actors, db=db)

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
