#!/usr/bin/env python3
"""PASS predicate for the LANDING goal-loop's Phase A: the job is WRITING.

job_predicate.py asks "has this job LEFT {plan, plan_sketching, failed}?" —
which a job wedged at `draft` passes VACUOUSLY, having never entered that
region (measured 2026-09-22 on job_1790005402603_0bd1f22b). This predicate
closes that hole by asking for the destination instead of the departure:
PASS = current_state in {generating, completed}. Entering the writing region
is exactly the thing the product does when the whole pre-writing pipeline
(draft form -> plan inputs -> planning burst -> plan_ready -> confirm)
succeeded, so it cannot be reached by skipping a stage.

Like its planning twin this reads the product's OWN FSM — a self-report — but
the bar the job must clear to get here is FROZEN for this loop via
MO_GOAL_PROTECTED_PATHS (the evidence floor, its enforcement, and the FSM
graph definition itself). The child cannot reach a pass by adding a
draft->generating edge or lowering a threshold it cannot edit.

`cancelled` is explicitly NOT a pass: an abandoned job has not been fixed
(job_terminal_fail.py is what stops the await for it).

Contract (recipes/goal-loop/lib/transforms.py::_await_terminal): invoked as
``python3 landing_predicate.py <run_uuid>`` (argv, not shell) inside
MO_GOAL_TARGET_CWD. Exit 0 == PASS. The FIRST stdout line is the reason.
Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_WRITING = {"generating", "completed"}
_DEAD = {"cancelled"}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: landing_predicate.py <run_uuid>", file=sys.stderr)
        return 2
    uuid = argv[0].strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        print(f"bad run uuid: {uuid!r}", file=sys.stderr)
        return 2

    proc = subprocess.run(
        [
            "psql",
            "-h", os.environ.get("PGHOST", "REDACTED-INTERNAL-IP"),
            "-p", os.environ.get("PGPORT", "5932"),
            "-U", os.environ.get("PGUSER", "researcher_user"),
            "-d", os.environ.get("PGDATABASE", "researcher_db"),
            "-tA", "-F", "|", "-c",
            "SELECT coalesce(f.current_state,'(none)'), "
            "coalesce(f.state_revision,0), "
            "coalesce(to_char(f.updated_at,'MM-DD HH24:MI'),'') "
            "FROM book_generation_runs r "
            "LEFT JOIN compose_job_fsm_state f ON f.job_id = r.id "
            f"WHERE r.id='{uuid}';",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # A DB blip is not a pass; stay conservative.
        print(f"{uuid[:8]} db-error (not pass): {proc.stderr.strip()[:80]}")
        return 1
    rows = proc.stdout.strip().splitlines()
    if not rows:
        print(f"{uuid[:8]} no run row (not pass)")
        return 1

    state, revision, updated = (rows[0].split("|") + [""] * 3)[:3]
    state_l = state.strip().lower()
    if state_l in _DEAD:
        print(f"{uuid[:8]} NOT PASS: state={state} (job abandoned, not fixed)")
        return 1
    if state_l in _WRITING:
        print(f"{uuid[:8]} PASS: writing region — state={state} "
              f"rev={revision} (last move {updated})")
        return 0
    print(f"{uuid[:8]} not pass: still pre-writing state={state} "
          f"rev={revision} (last move {updated})")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
