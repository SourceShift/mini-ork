#!/usr/bin/env python3
# verifiers/audit_coverage.py — prove every planned registry item was audited.
#
# Verifier contract:
#   * Reads <run_dir>/audit-plan.json (the wave's work list) and
#     <run_dir>/audit-result.json (the fan-out manifest).
#   * For every planned item, requires a checkpoint file at the item's recorded
#     result_path that parses and carries a status.
#   * Writes <run_dir>/audit-verdict.json:
#       {"verdict": "pass"|"fail", "reason": str, "total": N,
#        "missing": [...], "failed": [...], "deferred": [...]}
#   * Exits 0 for BOTH pass and fail — an incomplete audit is a VALID wave
#     result the revise edge acts on. It exits non-zero ONLY when an input is
#     unreadable, because then the verdict is undecidable, not "fail".
#   * Zero planned items is a pass ONLY when the registry was non-empty (every
#     item was already audited in an earlier wave). A zero-item registry is a
#     broken parse and fails, so the run can never "pass" by auditing nothing.
#
# Reads MINI_ORK_RUN_DIR + MINI_ORK_VERIFIER_EVIDENCE from the env exactly like
# recipes/goal-loop/verifiers/goal_check.py. Prints its verdict to stdout: the
# executor grades a verifier_ref on captured stdout, so an empty capture reads
# as a vacuous pass.

from __future__ import annotations

import json
import os
import sys

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
EVIDENCE = os.environ.get("MINI_ORK_VERIFIER_EVIDENCE") or os.path.join(
    RUN_DIR, "verifier-audit-coverage.log"
)
VERDICT_PATH = os.path.join(RUN_DIR, "audit-verdict.json")

PLAN = os.path.join(RUN_DIR, "audit-plan.json")
RESULT = os.path.join(RUN_DIR, "audit-result.json")


def _evidence(msg: str) -> None:
    with open(EVIDENCE, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    try:
        plan = _load(PLAN)
        result = _load(RESULT)
    except (OSError, json.JSONDecodeError) as exc:
        # Undecidable, not a clean fail: the wave never produced its inputs.
        print(f"audit_coverage: cannot read wave inputs: {exc}", file=sys.stderr)
        return 2

    pending = plan.get("pending", [])
    total_items = int(plan.get("total_items", 0) or 0)

    missing: list[str] = []
    for item in pending:
        item_id = item.get("id", "")
        path = item.get("result_path", "")
        if not path or not os.path.isfile(path):
            missing.append(item_id)
            continue
        try:
            payload = _load(path)
        except (OSError, json.JSONDecodeError):
            missing.append(item_id)
            continue
        if not payload.get("status"):
            missing.append(item_id)

    failed = [o["item_id"] for o in result.get("items", []) if o.get("status") == "failed"]
    timed_out = [o["item_id"] for o in result.get("items", []) if o.get("status") == "timeout"]
    deferred = [o["item_id"] for o in result.get("items", []) if o.get("status") == "deferred"]

    if total_items == 0:
        verdict, reason = "fail", "no_items"
    elif missing or failed or timed_out or deferred:
        verdict, reason = "fail", "incomplete"
    else:
        verdict, reason = "pass", "complete" if pending else "nothing_pending"

    payload = {
        "verdict": verdict,
        "reason": reason,
        "total_items": total_items,
        "planned": len(pending),
        "missing": sorted(missing),
        "failed": sorted(failed),
        "timed_out": sorted(timed_out),
        "deferred": sorted(deferred),
    }

    with open(VERDICT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")

    _evidence(
        f"verdict={verdict} reason={reason} total={total_items} planned={len(pending)} "
        f"missing={len(missing)} failed={len(failed)} deferred={len(deferred)}"
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
