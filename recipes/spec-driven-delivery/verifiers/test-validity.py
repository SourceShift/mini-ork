#!/usr/bin/env python3
"""test-validity — broken-baseline check on test_author's gates.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (test_validity; rule 3,
test-first). For every probe in ${MINI_ORK_RUN_DIR}/gates/<spec_id>.json:
each kind=cmd probe runs on the CURRENT, untouched tree with timeout
MO_SDD_SMOKE_TIMEOUT_S (default 120). A probe tagged `precondition` must pass
now; any other probe that passes now is vacuous and fails the gate. Static
vacuity checks for all kinds: probe non-empty and not `true`/`exit 0`/`:`,
`expect` non-empty. Spec coverage: every SpecCard acceptance id has exactly
one probe. Results go to ${MINI_ORK_RUN_DIR}/test-validity.json.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

"Passes" is the shared definition in _sdd_common.expect_matches (exit 0 AND
expect satisfied), the same one smoke-live applies later. Probes run with
the MO_SDD_SURFACE_ENV dotenv merged over the env, like smoke. A timed-out
non-precondition probe counts as not passing (recorded); a timed-out
precondition fails. Also per spec: the gates file's spec_id and source_hash
match the card, `unprobeable` is empty, each probe's kind matches its card
gate's kind, and no probe names an unknown acceptance id. An expect that any
output satisfies is vacuous. Statically vacuous probes are never executed.
Zero cards exits 2; zero probes fails. Specs named in MO_SDD_DELIVERED_SPECS
(comma list) are already-delivered: their passing probes are DELIVERED_OK,
not vacuous — recompile cycles after partial delivery need this.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import (  # noqa: E402
    atomic_write_json,
    is_vacuous_probe,
    load_cards,
    probe_env,
    probe_timeout,
    run_dir,
    run_probe,
    try_load_json,
    vacuous_expect,
)


def _probe_row(spec_id: str, probe: dict) -> dict:
    return {"spec_id": spec_id, "gate_id": probe.get("gate_id"), "acceptance_ref": probe.get("acceptance_ref"),
            "kind": probe.get("kind"), "tags": probe.get("tags") or [], "executed": False,
            "status": None, "reason": None, "exit_code": None, "duration_s": None, "output_tail": ""}


def check_spec(rd: Path, card: dict, *, timeout: float, env: dict) -> tuple[list[str], list[dict]]:
    sid = card["spec_id"]
    gates, problem = try_load_json(rd / "gates" / f"{sid}.json")
    if problem:
        return [f"gates file: {problem}"], []
    if not isinstance(gates, dict) or not isinstance(gates.get("probes"), list):
        return ["gates file: expected an object with a 'probes' list"], []
    out: list[str] = []
    if gates.get("spec_id") != sid:
        out.append("gates file spec_id does not match the card")
    if gates.get("source_hash") != card["source_hash"]:
        out.append("gates file source_hash does not match the card")
    for item in gates.get("unprobeable") or []:
        ref = item.get("acceptance_ref") if isinstance(item, dict) else item
        out.append(f"acceptance '{ref}' is unprobeable; the contract must be revised")

    kinds = {a["id"]: a["gate"]["kind"] for a in card["acceptance"]}
    seen: dict[str, int] = {}
    rows = []
    for probe in gates["probes"]:
        if not isinstance(probe, dict):
            out.append("probe entry is not an object")
            continue
        row = _probe_row(sid, probe)
        rows.append(row)
        ref = probe.get("acceptance_ref")
        problems = []
        if not isinstance(ref, str) or ref not in kinds:
            problems.append(f"acceptance_ref '{ref}' is not in the SpecCard")
        else:
            seen[ref] = seen.get(ref, 0) + 1
            if probe.get("kind") != kinds[ref]:
                problems.append(f"kind '{probe.get('kind')}' != card gate kind '{kinds[ref]}'")
        if is_vacuous_probe(probe.get("probe")):
            problems.append("vacuous probe (empty, true, :, or exit 0)")
        bad_expect = vacuous_expect(probe.get("expect"))
        if bad_expect:
            problems.append(f"vacuous expect: {bad_expect}")
        if not problems and probe.get("kind") == "cmd":
            problems.extend(_execute(row, probe, timeout=timeout, env=env))
        if problems:
            row["status"] = row["status"] or "INVALID"
            row["reason"] = "; ".join(problems)
            out.extend(f"{row['gate_id'] or ref}: {p}" for p in problems)
        elif not row["executed"]:
            row["status"], row["reason"] = "STATIC_OK", f"kind {probe.get('kind')}: static checks only"
    for aid in kinds:
        if seen.get(aid, 0) != 1:
            out.append(f"acceptance '{aid}' has {seen.get(aid, 0)} probes, need exactly 1")
    return out, rows


def _execute(row: dict, probe: dict, *, timeout: float, env: dict) -> list[str]:
    res = run_probe(probe["probe"], probe["expect"], timeout=timeout, env=env, cwd=os.getcwd())
    row.update(executed=True, exit_code=res["exit_code"], duration_s=res["duration_s"],
               output_tail=res["output_tail"])
    precondition = "precondition" in (probe.get("tags") or [])
    passed = res["status"] == "PASSED"
    if precondition:
        row["status"] = "PRECONDITION_OK" if passed else "PRECONDITION_FAILED"
        row["reason"] = res["reason"]
        return [] if passed else [f"precondition probe does not pass now ({res['reason']})"]
    delivered = {s.strip() for s in os.environ.get("MO_SDD_DELIVERED_SPECS", "").split(",") if s.strip()}
    if passed and row["spec_id"] in delivered:
        row["status"], row["reason"] = "DELIVERED_OK", "spec already delivered (MO_SDD_DELIVERED_SPECS); passing now is the expected state"
        return []
    row["status"] = "VACUOUS" if passed else "FAILS_TODAY"
    row["reason"] = res["reason"] if not passed else "passes on the untouched tree"
    return ["probe already passes on the untouched tree (vacuous)"] if passed else []


def body():
    rd = run_dir()
    cards = load_cards(rd)
    timeout = probe_timeout()
    env, surface_keys = probe_env()
    specs, rows = {}, []
    for sid, card in sorted(cards.items()):
        violations, spec_rows = check_spec(rd, card, timeout=timeout, env=env)
        specs[sid] = {"ok": not violations, "violations": violations}
        rows.extend(spec_rows)
    counts = {"probes": len(rows), "executed": sum(r["executed"] for r in rows),
              "vacuous": sum(r["status"] == "VACUOUS" for r in rows),
              "fails_today": sum(r["status"] == "FAILS_TODAY" for r in rows),
              "timeouts": sum(r["reason"] == "timeout" for r in rows)}
    atomic_write_json(rd / "test-validity.json", {"specs": specs, "probes": rows, "counts": counts,
                                                  "timeout_s": timeout, "surface_env_keys": surface_keys})
    failing = [sid for sid, s in specs.items() if not s["ok"]]
    detail = {"counts": counts, "specs": specs, "results_path": str(rd / "test-validity.json")}
    if failing:
        return False, f"{len(failing)} spec(s) have invalid gates; {failing[0]}: " \
                      f"{specs[failing[0]]['violations'][0]}", detail
    if not rows:
        return False, "no probes to validate", detail
    return True, f"{len(rows)} probe(s) valid ({counts['fails_today']} fail today as required)", detail


if __name__ == "__main__":
    from _sdd_common import run_main

    sys.exit(run_main(body))
