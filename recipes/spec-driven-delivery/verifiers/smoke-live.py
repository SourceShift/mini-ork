#!/usr/bin/env python3
"""smoke-live — execute every acceptance gate against the live surface.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (smoke_live; rule 4,
gates execute behavior). kind=cmd and kind=contract probes run as
subprocesses with the MO_SDD_SURFACE_ENV dotenv loaded (base URLs, tokens)
and a MO_SDD_SMOKE_TIMEOUT_S timeout (default 120; a hung probe is a FAIL
with reason timeout). A gate passes iff exit 0 and the output satisfies
`expect`. kind=ui probes go through the MO_SDD_UI_PROBE_CMD template when it
is set, else the gate is WAIVED (recorded, not passed). Verdicts are atomic
per gate with no partial credit: the node passes iff zero gates FAILED, and
the WAIVED count is recorded. Results go to ${MINI_ORK_RUN_DIR}/smoke-live.json.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

Probes come from ${MINI_ORK_RUN_DIR}/gates/<spec_id>.json (test_author's
output) and are cross-checked against the SpecCard: a card acceptance id with
no probe, a probe for an unknown acceptance id, a kind mismatch, or a
statically vacuous probe is a FAILED gate. "Passes" is the shared
_sdd_common.expect_matches definition test-validity also uses. The
MO_SDD_UI_PROBE_CMD placeholders {probe} {spec_id} {gate_id} {run_dir} are
replaced with shell-quoted values. deliverable_refs in the results come from
the card (deliverables whose acceptance_refs name the gate). Zero cards or a
missing/unreadable gates file exits 2; zero gates fails.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import (  # noqa: E402
    Malformed,
    atomic_write_json,
    deliverables_for,
    is_vacuous_probe,
    load_cards,
    load_json,
    probe_env,
    probe_timeout,
    run_dir,
    run_probe,
    shell_template,
)


def _gate(sid: str, gate_id, acceptance_ref, card: dict, kind) -> dict:
    return {"spec_id": sid, "gate_id": gate_id, "acceptance_ref": acceptance_ref, "kind": kind,
            "deliverable_refs": deliverables_for(card, acceptance_ref) if isinstance(acceptance_ref, str) else [],
            "status": None, "reason": None, "exit_code": None, "duration_s": None, "output_tail": ""}


def run_gate(gate: dict, probe: dict, kinds: dict, *, rd: Path, timeout: float, env: dict) -> None:
    ref, kind = probe.get("acceptance_ref"), probe.get("kind")
    if ref not in kinds:
        gate.update(status="FAILED", reason=f"acceptance_ref '{ref}' is not in the SpecCard")
        return
    if kind != kinds[ref]:
        gate.update(status="FAILED", reason=f"kind '{kind}' != card gate kind '{kinds[ref]}'")
        return
    text = probe.get("probe")
    if is_vacuous_probe(text):
        gate.update(status="FAILED", reason="vacuous probe")
        return
    if kind == "ui":
        template = os.environ.get("MO_SDD_UI_PROBE_CMD", "").strip()
        if not template:
            gate.update(status="WAIVED", reason="kind ui and MO_SDD_UI_PROBE_CMD is unset")
            return
        text = shell_template(template, {"probe": text, "spec_id": gate["spec_id"],
                                         "gate_id": str(gate["gate_id"]), "run_dir": str(rd)})
    res = run_probe(str(text), probe.get("expect") or "", timeout=timeout, env=env, cwd=os.getcwd())
    gate.update(status=res["status"], reason=res["reason"], exit_code=res["exit_code"],
                duration_s=res["duration_s"], output_tail=res["output_tail"])


def body():
    rd = run_dir()
    cards = load_cards(rd)
    timeout = probe_timeout()
    env, surface_keys = probe_env()
    gates = []
    for sid, card in sorted(cards.items()):
        doc = load_json(rd / "gates" / f"{sid}.json")
        if not isinstance(doc, dict) or not isinstance(doc.get("probes"), list):
            raise Malformed(f"gates/{sid}.json: expected an object with a 'probes' list")
        kinds = {a["id"]: a["gate"]["kind"] for a in card["acceptance"]}
        covered = set()
        for probe in doc["probes"]:
            if not isinstance(probe, dict):
                raise Malformed(f"gates/{sid}.json: probe entry is not an object")
            gate = _gate(sid, probe.get("gate_id") or probe.get("acceptance_ref"),
                         probe.get("acceptance_ref"), card, probe.get("kind"))
            covered.add(probe.get("acceptance_ref"))
            run_gate(gate, probe, kinds, rd=rd, timeout=timeout, env=env)
            gates.append(gate)
        for aid in kinds:
            if aid not in covered:
                gate = _gate(sid, aid, aid, card, kinds[aid])
                gate.update(status="FAILED", reason="no probe for this acceptance id")
                gates.append(gate)

    counts = {"total": len(gates), "passed": sum(g["status"] == "PASSED" for g in gates),
              "failed": sum(g["status"] == "FAILED" for g in gates),
              "waived": sum(g["status"] == "WAIVED" for g in gates)}
    atomic_write_json(rd / "smoke-live.json", {"gates": gates, "counts": counts, "timeout_s": timeout,
                                               "surface_env_keys": surface_keys})
    summary = [{k: g[k] for k in ("spec_id", "gate_id", "kind", "status", "reason")} for g in gates]
    detail = {"counts": counts, "waived": counts["waived"], "gates": summary[:50],
              "results_path": str(rd / "smoke-live.json")}
    if not gates:
        return False, "no gates to smoke", detail
    if counts["failed"]:
        first = next(g for g in gates if g["status"] == "FAILED")
        return False, (f"{counts['failed']}/{counts['total']} gate(s) FAILED; {first['spec_id']}/"
                       f"{first['gate_id']}: {first['reason']}"), detail
    return True, (f"{counts['passed']}/{counts['total']} gate(s) passed live, "
                  f"{counts['waived']} WAIVED"), detail


if __name__ == "__main__":
    from _sdd_common import run_main

    sys.exit(run_main(body))
