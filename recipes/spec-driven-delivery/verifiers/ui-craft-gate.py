#!/usr/bin/env python3
"""ui-craft-gate — conditional UI/UX craft judge.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (ui_craft_gate).
Applies only to SpecCards with ui_craft.required = true. Runs the
MO_SDD_UI_GATE_CMD template (vars {spec_id}, {design_sources}, {run_dir});
a nonzero exit fails. An empty MO_SDD_UI_GATE_CMD means the gate is skipped
with a WAIVED verdict (pass true, "waived": true) that ledger_writer records
as a WAIVED ledger row, counted by the slide-back gauge.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

Per-spec results (PASSED|FAILED|WAIVED|NOT_REQUIRED) go to
${MINI_ORK_RUN_DIR}/ui-craft.json, the receipted input ledger_writer reads.
Placeholders are replaced with shell-quoted values ({design_sources} becomes
space-separated quoted paths). Each run has a MO_SDD_UI_GATE_TIMEOUT_S
timeout (default 600) with a process-group kill; a timeout fails. No card
requiring ui_craft passes with reason 'no ui_craft cards'. Zero cards exits 2.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import (  # noqa: E402
    OUTPUT_TAIL_CHARS,
    atomic_write_json,
    load_cards,
    positive_float_env,
    run_cmd,
    run_dir,
    shell_template,
)


def body():
    rd = run_dir()
    cards = load_cards(rd)
    template = os.environ.get("MO_SDD_UI_GATE_CMD", "").strip()
    timeout = positive_float_env("MO_SDD_UI_GATE_TIMEOUT_S", 600.0)
    required = [sid for sid, c in sorted(cards.items()) if c["ui_craft"]["required"]]
    specs = []
    for sid, card in sorted(cards.items()):
        row = {"spec_id": sid, "status": "NOT_REQUIRED", "reason": "ui_craft.required is false",
               "exit_code": None, "duration_s": None, "output_tail": ""}
        if sid in required and not template:
            row.update(status="WAIVED", reason="MO_SDD_UI_GATE_CMD is empty")
        elif sid in required:
            cmd = shell_template(template, {"spec_id": sid, "run_dir": str(rd),
                                            "design_sources": list(card["ui_craft"]["design_sources"])})
            res = run_cmd(["bash", "-c", cmd], timeout=timeout, env=dict(os.environ), cwd=os.getcwd())
            ok = res["exit_code"] == 0
            reason = res["error"] or ("timeout" if res["timed_out"] else f"exit {res['exit_code']}")
            row.update(status="PASSED" if ok else "FAILED", reason=reason, exit_code=res["exit_code"],
                       duration_s=res["duration_s"], output_tail=res["output"][-OUTPUT_TAIL_CHARS:])
        specs.append(row)

    counts = {s: sum(r["status"] == s for r in specs) for s in ("PASSED", "FAILED", "WAIVED", "NOT_REQUIRED")}
    waived = counts["WAIVED"] > 0
    atomic_write_json(rd / "ui-craft.json", {"specs": specs, "counts": counts, "waived": waived,
                                             "required": required})
    detail = {"required": required, "counts": counts, "waived": waived,
              "specs": [{k: r[k] for k in ("spec_id", "status", "reason")} for r in specs],
              "results_path": str(rd / "ui-craft.json")}
    if not required:
        return True, "no ui_craft cards", detail
    if counts["FAILED"]:
        first = next(r for r in specs if r["status"] == "FAILED")
        return False, f"ui craft gate FAILED for {first['spec_id']}: {first['reason']}", detail
    if waived:
        return True, f"ui craft gate WAIVED for {counts['WAIVED']} spec(s) (MO_SDD_UI_GATE_CMD empty)", detail
    return True, f"ui craft gate passed for {counts['PASSED']} spec(s)", detail


if __name__ == "__main__":
    from _sdd_common import run_main

    sys.exit(run_main(body))
