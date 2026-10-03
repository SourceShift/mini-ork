#!/usr/bin/env python3
"""ledger-writer — traceability ledger and slide-back gauges.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (ledger_writer; rule 6).
Appends rows to ${MINI_ORK_RUN_DIR}/ledger.jsonl:
{ts, spec_id, clause_id, deliverable_id, child_run_id, commit, gate_id,
verdict, waived}, including WAIVED gates and human veto events. Computes
slide-back gauges {waived_gates, failed_gates, asks_open} (plus the
spec-edit vs code-edit ratio) into ${MINI_ORK_RUN_DIR}/slide-back.json.
Fails only on missing or corrupt inputs.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

Inputs: spec-cards/, smoke-live.json, aggregate-verdict.json, and
ui-craft.json when any card has ui_craft.required; a missing or corrupt one
exits 2, as does a smoke-live.json with no gates. One row per smoke gate x clause_ref x deliverable (clause and
deliverable links come from the SpecCard; child_run_id and commit from the
recomputed aggregate-verdict.json, never from dispatch-results.json), one
gate_id 'ui_craft' row per ui-craft PASSED/FAILED/WAIVED spec, and one
gate_id 'veto' row (verdict VETOED) per ${MINI_ORK_RUN_DIR}/vetoes/*.json
event ({spec_id, deliverable_id?, reason}). Rows are appended, never
truncated. Gauges cover this invocation's rows and count distinct
(spec_id, gate_id) gates: waived_gates, failed_gates, vetoes, plus asks_open
(asks/*.json without resolved: true or a non-empty answer; an unreadable ASK
counts as open). spec_edit_ratio is null until a deterministic source for it
exists. slide-back.json is written atomically. Non-zero gauges still pass.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import (  # noqa: E402
    Malformed,
    atomic_write_json,
    deliverables_for,
    load_cards,
    load_json,
    run_dir,
    try_load_json,
    utc_now,
)

_SMOKE_VERDICTS = {"PASSED", "FAILED", "WAIVED"}


def _list_of_dicts(doc, key: str, name: str) -> list[dict]:
    if not isinstance(doc, dict) or not isinstance(doc.get(key), list) \
            or not all(isinstance(x, dict) for x in doc[key]):
        raise Malformed(f"{name}: expected an object with a '{key}' list of objects")
    return doc[key]


def _row(ts, sid, clause_id, deliverable_id, link, gate_id, verdict) -> dict:
    return {"ts": ts, "spec_id": sid, "clause_id": clause_id, "deliverable_id": deliverable_id,
            "child_run_id": link.get("child_run_id"), "commit": link.get("commit"),
            "gate_id": gate_id, "verdict": verdict, "waived": verdict == "WAIVED"}


def _asks_open(rd: Path) -> tuple[int, list[str]]:
    open_asks = []
    for path in sorted((rd / "asks").glob("*.json")):
        ask, _ = try_load_json(path)
        resolved = isinstance(ask, dict) and (ask.get("resolved") is True or bool(str(ask.get("answer") or "").strip()))
        if not resolved:
            open_asks.append(path.name)
    return len(open_asks), open_asks


def body():
    rd = run_dir()
    cards = load_cards(rd)
    smoke = _list_of_dicts(load_json(rd / "smoke-live.json"), "gates", "smoke-live.json")
    agg = _list_of_dicts(load_json(rd / "aggregate-verdict.json"), "deliverables", "aggregate-verdict.json")
    needs_ui = any(c["ui_craft"]["required"] for c in cards.values())
    ui = _list_of_dicts(load_json(rd / "ui-craft.json"), "specs", "ui-craft.json") if needs_ui else []
    if not smoke:
        raise Malformed("smoke-live.json lists no gates")
    links = {(r.get("spec_id"), r.get("deliverable_id")): r for r in agg}

    ts = utc_now()
    rows = []
    for gate in smoke:
        sid, verdict = gate.get("spec_id"), gate.get("status")
        if sid not in cards:
            raise Malformed(f"smoke-live.json gate names unknown spec_id {sid!r}")
        if verdict not in _SMOKE_VERDICTS:
            raise Malformed(f"smoke-live.json gate {sid}/{gate.get('gate_id')} has status {verdict!r}")
        card, ref = cards[sid], gate.get("acceptance_ref")
        acc = next((a for a in card["acceptance"] if a["id"] == ref), None)
        clause_ids = list(acc["clause_refs"]) if acc and acc["clause_refs"] else [None]
        deliverable_ids = (deliverables_for(card, ref) if isinstance(ref, str) else []) or [None]
        for clause_id in clause_ids:
            for did in deliverable_ids:
                rows.append(_row(ts, sid, clause_id, did, links.get((sid, did), {}),
                                 gate.get("gate_id") or ref, verdict))
    for entry in ui:
        status = entry.get("status")
        if entry.get("spec_id") not in cards:
            raise Malformed(f"ui-craft.json names unknown spec_id {entry.get('spec_id')!r}")
        if status in _SMOKE_VERDICTS:
            rows.append(_row(ts, entry.get("spec_id"), None, None, {}, "ui_craft", status))
        elif status != "NOT_REQUIRED":
            raise Malformed(f"ui-craft.json spec {entry.get('spec_id')!r} has status {status!r}")
    vetoes = 0
    for path in sorted((rd / "vetoes").glob("*.json")):
        event = load_json(path)
        if not isinstance(event, dict) or not event.get("spec_id"):
            raise Malformed(f"vetoes/{path.name}: expected an object with a spec_id")
        did = event.get("deliverable_id")
        rows.append(_row(ts, event["spec_id"], None, did, links.get((event["spec_id"], did), {}),
                         "veto", "VETOED"))
        vetoes += 1
    rows.sort(key=lambda r: (str(r["spec_id"]), str(r["gate_id"]), str(r["clause_id"]),
                             str(r["deliverable_id"])))

    ledger = rd / "ledger.jsonl"
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        fh.flush()
        os.fsync(fh.fileno())

    def distinct(verdict):
        return len({(r["spec_id"], r["gate_id"]) for r in rows if r["verdict"] == verdict})

    asks_open, open_names = _asks_open(rd)
    gauges = {"waived_gates": distinct("WAIVED"), "failed_gates": distinct("FAILED"),
              "vetoes": vetoes, "asks_open": asks_open, "spec_edit_ratio": None,
              "spec_edit_ratio_reason": "no deterministic spec-edit vs code-edit source yet"}
    atomic_write_json(rd / "slide-back.json", {"ts": ts, **gauges, "rows_written": len(rows),
                                               "asks_open_files": open_names})
    detail = {"gauges": gauges, "rows_written": len(rows), "ledger_path": str(ledger),
              "slide_back_path": str(rd / "slide-back.json")}
    return True, (f"{len(rows)} ledger row(s) appended; waived_gates={gauges['waived_gates']} "
                  f"failed_gates={gauges['failed_gates']} asks_open={asks_open}"), detail


if __name__ == "__main__":
    from _sdd_common import run_main

    sys.exit(run_main(body))
