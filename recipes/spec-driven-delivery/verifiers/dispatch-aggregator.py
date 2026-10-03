#!/usr/bin/env python3
"""dispatch-aggregator — recompute per-deliverable verdicts from child runs.

Design: docs/plans/2026-10-03-spec-driven-delivery.md (dispatch_aggregator;
rule 2, the verifier payload is the only verdict). Reads
${MINI_ORK_RUN_DIR}/dispatch-results.json and writes
${MINI_ORK_RUN_DIR}/aggregate-verdict.json with shape
{total, delivered, failed, blocked, pending, pass_rate, deliverables[]}.
Never trusts the dispatcher's claims: every non-blocked deliverable needs a
child_run_id whose child_run_dir exists and holds a non-empty verdict
artifact, and its status is recomputed from that artifact. Every blocked
deliverable needs an ASK at its recorded ask_path,
${MINI_ORK_RUN_DIR}/asks/<spec_id>--<deliverable_id>.json. Pass iff every
deliverable is delivered.

Verdict: exactly one JSON line on stdout, {"pass": bool, "reason": str, ...};
the executor reads that payload as the node verdict. Exit 0 pass, 1 fail,
2 malformed input. Runs with cwd = target repo and MINI_ORK_RUN_DIR,
MINI_ORK_PLAN_PATH, ARTIFACT_PATH in the environment.

The expected deliverable set comes from the SpecCards: a card deliverable
with no dispatch record is pending (omission cannot hide a failure), and a
record naming no card deliverable, or a duplicate record, is failed. The
verdict_path (relative paths resolve against child_run_dir) must realpath
inside child_run_dir and be non-empty. Accepted verdict shapes: a JSON object
(or, for log-prefixed evidence, its last JSON-object line) with a boolean
`pass` (verifier payloads such as tier4-panel-quorum), else a `verdict` string
(pass/approve -> delivered; fail/request_changes/escalate -> failed).
Anything else is pending. A blocked claim with a valid ASK (non-empty
`question`) recomputes to blocked; without one it is failed. `commit` comes
from the verdict or the child's implementer-summary.json when it looks like a
git sha, else null. Each row records claimed_status vs status and every
mismatch. aggregate-verdict.json is written atomically.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdd_common import (  # noqa: E402
    Malformed,
    atomic_write_json,
    is_within,
    load_cards,
    load_json,
    run_dir,
    try_load_json,
)

_DELIVERED = {"pass", "passed", "approve", "approved", "delivered"}
_FAILED = {"fail", "failed", "request_changes", "escalate", "reject", "rejected"}
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def parse_verdict(path: Path) -> tuple[str, str, dict | None]:
    """(status, reason, verdict object) from one child verdict artifact."""
    text = path.read_text(encoding="utf-8", errors="replace")
    obj = None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    if not isinstance(obj, dict):
        return "pending", "child verdict is not a JSON object", None
    if isinstance(obj.get("pass"), bool):
        return ("delivered", "child verdict pass: true", obj) if obj["pass"] else \
            ("failed", "child verdict pass: false", obj)
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict in _DELIVERED:
        return "delivered", f"child verdict '{obj['verdict']}'", obj
    if verdict in _FAILED:
        return "failed", f"child verdict '{obj['verdict']}'", obj
    return "pending", "child verdict has no recognised pass/verdict field", obj


def _commit(verdict: dict | None, child_dir: Path) -> str | None:
    candidates = [verdict.get("commit") if verdict else None]
    summary, _ = try_load_json(child_dir / "implementer-summary.json")
    if isinstance(summary, dict):
        candidates.append(summary.get("commit"))
    for value in candidates:
        if isinstance(value, str) and _SHA_RE.fullmatch(value.strip().lower()):
            return value.strip().lower()
    return None


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def check_blocked(rd: Path, rec: dict, sid: str, did: str) -> tuple[str, str]:
    expected = rd / "asks" / f"{sid}--{did}.json"
    ask_path = _str(rec.get("ask_path"))
    if not ask_path:
        return "failed", "claimed blocked with no ask_path"
    if os.path.realpath(ask_path) != os.path.realpath(expected):
        return "failed", f"ask_path is not {expected}"
    ask, problem = try_load_json(expected)
    if problem:
        return "failed", f"claimed blocked but ASK unusable: {problem}"
    if not isinstance(ask, dict) or not _str(ask.get("question")):
        return "failed", "claimed blocked but ASK has no question"
    for key, want in (("spec_id", sid), ("deliverable_id", did)):
        if key in ask and ask[key] != want:
            return "failed", f"ASK {key} '{ask[key]}' != '{want}'"
    return "blocked", "ASK present"


def check_child(rec: dict) -> tuple[str, str, str | None]:
    run_id = _str(rec.get("child_run_id"))
    child = _str(rec.get("child_run_dir"))
    vpath = _str(rec.get("verdict_path"))
    if not run_id:
        return "pending", "no child_run_id", None
    if not child or not Path(child).is_dir():
        return "pending", "child_run_dir missing", None
    if not vpath:
        return "pending", "no verdict_path", None
    verdict_file = Path(vpath) if os.path.isabs(vpath) else Path(child) / vpath
    if not is_within(verdict_file, child):
        return "pending", "verdict_path is outside child_run_dir", None
    if not verdict_file.is_file() or verdict_file.stat().st_size == 0:
        return "pending", "child verdict artifact missing or empty", None
    status, reason, verdict = parse_verdict(verdict_file)
    return status, reason, _commit(verdict, Path(child))


def body():
    rd = run_dir()
    record = load_json(rd / "dispatch-results.json")
    if not isinstance(record, dict) or not isinstance(record.get("deliverables"), list):
        raise Malformed("dispatch-results.json: expected an object with a 'deliverables' list")
    cards = load_cards(rd)
    expected = [(sid, d["id"]) for sid, c in sorted(cards.items()) for d in c["deliverables"]]

    records: dict[tuple[str, str], list[dict]] = {}
    for rec in record["deliverables"]:
        if not isinstance(rec, dict):
            raise Malformed("dispatch-results.json: deliverable entry is not an object")
        records.setdefault((str(rec.get("spec_id")), str(rec.get("deliverable_id"))), []).append(rec)
    keys = expected + sorted(set(records) - set(expected))

    rows = []
    for sid, did in keys:
        recs = records.get((sid, did), [])
        rec = recs[0] if recs else {}
        claimed = rec.get("status") if recs else None
        row = {"spec_id": sid, "deliverable_id": did, "claimed_status": claimed,
               "child_run_id": _str(rec.get("child_run_id")) or None,
               "child_run_dir": _str(rec.get("child_run_dir")) or None,
               "verdict_path": _str(rec.get("verdict_path")) or None, "commit": None}
        if (sid, did) not in expected:
            status, reason = "failed", "dispatch record names no SpecCard deliverable"
        elif not recs:
            status, reason = "pending", "no dispatch record"
        elif len(recs) > 1:
            status, reason = "failed", f"{len(recs)} dispatch records for one deliverable"
        elif claimed == "blocked":
            status, reason = check_blocked(rd, rec, sid, did)
        else:
            status, reason, row["commit"] = check_child(rec)
        row.update(status=status, reason=reason, mismatch=claimed != status)
        if row["mismatch"]:
            row["mismatch_reason"] = f"claimed {claimed or 'nothing'}, recomputed {status}: {reason}"
        rows.append(row)

    counts = {k: sum(r["status"] == k for r in rows) for k in ("delivered", "failed", "blocked", "pending")}
    total = len(rows)
    aggregate = {"total": total, **counts,
                 "pass_rate": round(counts["delivered"] / total, 4) if total else 0,
                 "deliverables": rows}
    atomic_write_json(rd / "aggregate-verdict.json", aggregate)
    mismatches = [r["mismatch_reason"] for r in rows if r["mismatch"]]
    detail = {"total": total, **counts, "pass_rate": aggregate["pass_rate"],
              "mismatches": mismatches[:20], "aggregate_path": str(rd / "aggregate-verdict.json")}
    if total == 0:
        return False, "no deliverables to aggregate", detail
    if counts["delivered"] != total:
        first = next(r for r in rows if r["status"] != "delivered")
        return False, (f"{counts['delivered']}/{total} delivered; {first['spec_id']}/"
                       f"{first['deliverable_id']} {first['status']}: {first['reason']}"), detail
    return True, f"{total}/{total} deliverables delivered (recomputed from child runs)", detail


if __name__ == "__main__":
    from _sdd_common import run_main

    sys.exit(run_main(body))
