#!/usr/bin/env python3
"""PASS predicate for the goal-loop on compose (planning) jobs.

The planning-phase twin of ``chapter_predicate.py``. The chapter predicate asks
"has this chapter committed AND passed its rubric?"; this one asks the only
question that matters one stage earlier: "has this job LEFT the planning region?"

Contract (recipes/goal-loop/lib/transforms.py::_await_terminal): invoked as
``python3 job_predicate.py <run_uuid>`` (argv, not shell) inside
MO_GOAL_TARGET_CWD. Exit 0 == PASS. Any non-zero == not yet. The FIRST stdout
line is captured as the reason.

WHY "LEFT THE PLANNING REGION" AND NOT "REACHED plan_ready". A repair can
legitimately carry the job past the wedge in more than one direction, and
pinning the predicate to a single target state would read a job that skipped
ahead as a failure. The planning region is ``plan`` / ``plan_sketching`` /
``failed`` — the states a wedged job cycles through — and leaving it in any
direction toward work is the pass. ``cancelled`` and ``paused`` are explicitly
NOT a pass: a cancelled job has not been fixed, and scoring it green would let
the loop bank a win on an abandoned unit (job_terminal_fail.py is what stops
the await for those).

This predicate reads the product's OWN FSM, so it is a self-report — but unlike
the chapter predicate there is no judge flag to launder: leaving the planning
region is exactly the thing the product does when planning succeeds, and the
bar it had to clear to get there (the per-topic evidence floor) is FROZEN for
this loop via MO_GOAL_PROTECTED_PATHS. The child cannot reach a pass by
lowering a threshold it cannot edit.

Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# The states a wedged planning job cycles through. Anywhere else = it moved.
_PLANNING_REGION = {"plan", "plan_sketching", "failed"}

# Not a pass, and not a wedge either: the unit is gone. Deliberately excluded
# from the pass set so this predicate never banks a green on an abandoned job;
# job_terminal_fail.py ends the await for these.
_DEAD_STATES = {"cancelled"}


def _q(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "-h", os.environ.get("PGHOST", "100.74.239.22"),
            "-p", os.environ.get("PGPORT", "5932"),
            "-U", os.environ.get("PGUSER", "researcher_user"),
            "-d", os.environ.get("PGDATABASE", "researcher_db"),
            "-tA", "-F", "|", "-c", sql,
        ],
        capture_output=True,
        text=True,
    )


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: job_predicate.py <run_uuid>", file=sys.stderr)
        return 2
    uuid = argv[0].strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        print(f"bad run uuid: {uuid!r}", file=sys.stderr)
        return 2

    proc = _q(
        "SELECT coalesce(f.current_state,'(none)'), "
        "coalesce(f.state_revision,0), "
        "coalesce(to_char(f.updated_at,'MM-DD HH24:MI'),'') "
        "FROM book_generation_runs r "
        "LEFT JOIN compose_job_fsm_state f ON f.job_id = r.id "
        f"WHERE r.id='{uuid}';"
    )
    if proc.returncode != 0:
        # A DB blip is not a pass. Stay conservative so a transient error cannot
        # settle the await on a job that never advanced.
        print(f"{uuid[:8]} db-error (not pass): {proc.stderr.strip()[:80]}")
        return 1

    rows = proc.stdout.strip().splitlines()
    if not rows:
        print(f"{uuid[:8]} no run row (not pass)")
        return 1

    state, revision, updated = (rows[0].split("|") + [""] * 3)[:3]
    state_l = state.strip().lower()

    if state_l in _DEAD_STATES:
        print(f"{uuid[:8]} NOT PASS: state={state} (job abandoned, not fixed)")
        return 1
    if state_l in _PLANNING_REGION:
        print(f"{uuid[:8]} not pass: still in the planning region "
              f"state={state} rev={revision} (last move {updated})")
        return 1
    print(f"{uuid[:8]} PASS: left the planning region — state={state} "
          f"rev={revision} (last move {updated})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
