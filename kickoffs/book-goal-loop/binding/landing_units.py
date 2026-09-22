#!/usr/bin/env python3
"""units-cmd for the LANDING goal-loop: emit the pinned run uuid while wedged.

The third stage binding. job_thrash.py detects wedges by burst-signature
repetition and so needs compose_planning_events rows; list_chapters.sh needs
book_chapter_lifecycle rows. A job parked in the DRAFT region has NEITHER —
measured 2026-09-22 on job_1790005402603_0bd1f22b: FSM `draft` rev 8, zero
planning events, zero lifecycle rows, 25 draft_form_data write revisions. Both
existing listers read that job as a clean board. This lister closes the blind
spot the cheap way: the OPERATOR pins the unit (MO_GOAL_LANDING_RUN_UUID), so
no statistical wedge signature is needed — "not yet writing" IS the wedge.

Contract (recipes/goal-loop/lib/goal_state.py::list_units): run via shell in
MO_GOAL_TARGET_CWD, one unit id per line, rc!=0 aborts the wave. Empty output
means the job reached the writing region (pair with MO_GOAL_EMPTY_UNITS_PASS=1).
Non-vacuous by construction: any DB failure exits 2, so an empty list is always
a clean read that found the job writing, never a blind spot.

WITHHELD-WHILE-BURSTING: `plan_sketching` with a fresh FSM heartbeat is healthy
planning in flight, not a wedge. Dispatching a fix child against it wastes a
wave and races the burst. The freshness window is MO_GOAL_LANDING_STALL_SECONDS
(default 900): a sketching job whose FSM row moved inside the window is
withheld; one stalled past it is emitted.

Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Every state before the writing region. `generating`/`completed` = landed
# (Phase A's goal); `cancelled` = dead (terminal-fail's business, not ours).
_PRE_WRITING = {
    "draft", "plan", "plan_sketching", "planning_paused",
    "plan_ready", "knowledge_calibration", "failed", "paused",
}
_ACTIVE_BURST_STATES = {"plan_sketching"}


def main() -> int:
    uuid = os.environ.get("MO_GOAL_LANDING_RUN_UUID", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        print(f"MO_GOAL_LANDING_RUN_UUID is not a uuid: {uuid!r}", file=sys.stderr)
        return 2
    stall_s = int(os.environ.get("MO_GOAL_LANDING_STALL_SECONDS", "900"))

    proc = subprocess.run(
        [
            "psql",
            "-h", os.environ.get("PGHOST", "REDACTED-INTERNAL-IP"),
            "-p", os.environ.get("PGPORT", "5932"),
            "-U", os.environ.get("PGUSER", "researcher_user"),
            "-d", os.environ.get("PGDATABASE", "researcher_db"),
            "-tA", "-F", "|", "-c",
            "SELECT coalesce(f.current_state,'(no fsm)'), "
            "coalesce(extract(epoch FROM now() - f.updated_at)::int, -1) "
            "FROM book_generation_runs r "
            "LEFT JOIN compose_job_fsm_state f ON f.job_id = r.id "
            f"WHERE r.id='{uuid}';",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"psql failed: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return 2
    rows = proc.stdout.strip().splitlines()
    if not rows:
        print(f"no book_generation_runs row for {uuid}", file=sys.stderr)
        return 2

    state, age_s = (rows[0].split("|") + [""])[:2]
    state_l = state.strip().lower()
    try:
        age = int(age_s)
    except ValueError:
        age = -1

    if state_l not in _PRE_WRITING:
        return 0  # writing / completed / cancelled — nothing to emit
    if state_l in _ACTIVE_BURST_STATES and 0 <= age < stall_s:
        # healthy burst in flight — withhold rather than race it
        return 0
    print(uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
