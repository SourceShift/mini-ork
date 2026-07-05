"""Python port of bin/mini-ork-promote — promotion gate CLI.

Strangler-fig parity port. Orchestrates the already-ported
``promotion_gate.promotion_evaluate`` + ``version_registry.register``: parses
args, runs the candidate-status preflight gates, evaluates the promotion
decision, and (unless --dry-run) acts on it — register version + mark promoted,
or mark quarantined. Exit codes match bash (0 ok, 1 register-fail, 2 usage/gate,
3 missing lib — not applicable in-process).

    main(argv=None, *, db=None, root=None) -> int
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

from . import promotion_gate, version_registry

_USAGE = """Usage: mini-ork promote --candidate <id> [--force] [--dry-run]

Run the promotion gate for a workflow candidate.

Decisions:
  promoted     → version_registry.sh:version_register is called; candidate goes live
  rejected     → candidate remains in evaluated state; no version bump
  quarantined  → candidate is permanently blocked; cannot be re-evaluated
                 (use: mini-ork version_clear_quarantine --candidate <id> to unblock)

Outputs PromotionDecision JSON on stdout.

Options:
  --candidate <id>   Candidate to evaluate (required)
  --force            Skip utility_delta threshold check (emergency promotion only)
  --dry-run          Compute decision; do not write DB or register version
  --help             Show this help
"""


def _resolve_db(db: str | None) -> str:
    if db:
        return db
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")


def main(argv: list[str] | None = None, *, db: str | None = None, root: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    candidate_id = ""
    force = 1 if os.environ.get("MINI_ORK_PROMOTE_FORCE") == "1" else 0
    dry_run = 1 if os.environ.get("MINI_ORK_DRY_RUN") == "1" else 0

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_USAGE); return 0
        elif a == "--dry-run":
            dry_run = 1; i += 1
        elif a == "--force":
            force = 1; i += 1
        elif a == "--candidate":
            if i + 1 >= len(argv):
                sys.stderr.write("--candidate requires an id\n"); return 2
            candidate_id = argv[i + 1]; i += 2
        elif a.startswith("-"):
            sys.stderr.write(f"Unknown flag: {a}. Try --help\n"); return 2
        else:
            sys.stderr.write(f"Unexpected argument: {a}. Try --help\n"); return 2
    if not candidate_id:
        sys.stdout.write(_USAGE); return 2

    db = _resolve_db(db)

    # ── preflight: candidate must exist + be past the eval gate ────────────
    status = utility_delta = None
    if os.path.isfile(db):
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT status, utility_delta FROM workflow_candidates WHERE candidate_id=? LIMIT 1",
            (candidate_id,)).fetchone()
        con.close()
        if row:
            status, utility_delta = row[0], row[1]
    if status is None:
        sys.stderr.write(f"Candidate not found: {candidate_id}\n")
        sys.stderr.write(f"Run 'mini-ork improve' then 'mini-ork eval --candidate {candidate_id}' first.\n")
        return 2
    if status == "quarantined":
        sys.stderr.write(f"Candidate is quarantined: {candidate_id}\nCannot promote a quarantined candidate.\n")
        return 2
    if status == "promoted":
        sys.stderr.write(f"Candidate already promoted: {candidate_id}\n")
        return 0
    if status == "candidate" and force == 0:
        sys.stderr.write(f"Candidate has not been evaluated: {candidate_id} (status={status})\n")
        sys.stderr.write(f"Run: mini-ork eval --candidate {candidate_id}\nOR --force to skip evaluation (emergency promotion)\n")
        return 2

    sys.stdout.write("=== mini-ork promote ===\n"
                     f"    candidate:     {candidate_id}\n"
                     f"    eval_status:   {status}\n"
                     f"    utility_delta: {utility_delta if utility_delta is not None else 0}\n"
                     f"    force:         {force}\n\n")

    os.environ["MINI_ORK_PROMOTE_FORCE"] = str(force)
    os.environ["MINI_ORK_PROMOTE_DRY_RUN"] = str(dry_run)
    decision_json = promotion_gate.promotion_evaluate(db, candidate_id)
    decision = decision_json.get("decision", "unknown")

    sys.stdout.write("PromotionDecision:\n" + json.dumps(decision_json, indent=4) + "\n\n")

    if dry_run == 1:
        sys.stdout.write(f"[dry-run] decision={decision} — no DB writes or version registration\n")
        return 0

    rc = 0
    if decision == "promoted":
        new_version = decision_json.get("version_id") or f"{candidate_id}-promoted"
        payload = json.dumps({"version_id": new_version, "name": candidate_id,
                              "status": "stable", "utility_score": float(utility_delta or 0)})
        try:
            version_registry.register("workflow", payload, db=db)
        except Exception:
            sys.stderr.write("version_register failed\n"); return 1
        con = sqlite3.connect(db)
        con.execute("UPDATE workflow_candidates SET status='promoted' WHERE candidate_id=?", (candidate_id,))
        con.commit(); con.close()
    elif decision == "quarantined":
        con = sqlite3.connect(db)
        con.execute("UPDATE workflow_candidates SET status='quarantined' WHERE candidate_id=?", (candidate_id,))
        con.commit(); con.close()
    else:
        sys.stdout.write(f"Decision: {decision} — no action taken (candidate remains evaluatable)\n")

    sys.stdout.write(f"\npromotion_decision={decision}\ncandidate_id={candidate_id}\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
