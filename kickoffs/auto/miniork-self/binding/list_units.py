#!/usr/bin/env python3
"""units-cmd for the mini-ork-self goal loop: emit the pinned unit inventory.

Contract (recipes/goal-loop/lib/goal_state.py::list_units and
verifiers/goal_check.py): run via shell in MO_GOAL_TARGET_CWD, one unit id per
line, rc!=0 aborts the wave. The list is the WATCH signal; the predicate decides
pass/fail per unit, exactly as list_chapters.sh emits all chapters regardless of
their state. Non-vacuous by construction: if the target tree does not look like
mini-ork, or the live db is unreadable, this exits 2 rather than emitting an
empty (and therefore silently green) list.

WHY THIS INVENTORY IS ONLY TWO UNITS. mini-ork's real pending/broken backlog was
enumerated on 2026-09-25 by asking what a check can actually measure, not by
asking what looks untidy:

  migrate-drift-comment-only     a comment reword in 0038 bumped its checksum,
                                 so `mini-ork update` returns 1 on every db that
                                 applied the pre-edit file and dies before
                                 reaching 0054/0055/0056/0058 — which is why
                                 four migrations have sat unapplied for months.
  migration-0054-drops-columns   0054's create-copy-drop-rename omits exactly
                                 route_margin and predicted_error. On the live
                                 db (where both already exist) applying it
                                 destroys route_margin — the entire training set
                                 the UCCI map is fit from.

Two more candidates were examined and DELIBERATELY EXCLUDED, because a unit that
cannot be made to pass is a permanently red wave, and a unit whose "fix" is not
code is not work:

  task-runs-cost-zero            goal-loop runs report $0.00 in
                                 task_runs.cost_usd. BLOCKED: the writer lives
                                 in mini_ork/cli/execute_handlers.py, claimed by
                                 the live worktree `lens-json-materialize`; a fix
                                 child here would race it. Re-open once that
                                 worktree lands.
  route-margin-sparse            only 2 of 53 learned/explore traces carry
                                 route_margin. NOT A DEFECT: the write chain
                                 (routing.py -> trace_store.trace_write) is
                                 intact and tested; the margin is legitimately
                                 None when the pick comes from fetch_global_best
                                 or the EquiRouter override, which is what the
                                 MO_LEARNING_MIN_SAMPLES=3 floor forces for most
                                 slices. The remedy is traffic, not code — it is
                                 task #12/#6, the campaign this loop feeds.

The consequence is stated plainly: this is a SHORT campaign. Two units, not a
hundred. mini-ork does not have a hundred pieces of real broken work, and
inventing them would generate churn rather than value.

Connection comes from the environment; no secret lives here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Pinned, in dispatch order. Both are fixable by a code-fix child in the target
# worktree, and both are measurable without touching the live db.
UNITS = (
    "migrate-drift-comment-only",
    "migration-0054-drops-columns",
)
_OTHERWISE_UNIT = {
    "task-runs-cost-zero": "blocked: execute_handlers.py claimed by wt/lens-json-materialize",
    "route-margin-sparse": "not a defect: data symptom of the min-samples floor (task #12)",
}


def main() -> int:
    cwd = os.environ.get("MO_GOAL_TARGET_CWD", "").strip()
    if not cwd or not os.path.isdir(cwd):
        print(f"MO_GOAL_TARGET_CWD unset or not a dir: {cwd!r}", file=sys.stderr)
        return 2

    repo = Path(cwd)
    if not (repo / "db" / "migrations").is_dir():
        print(f"not a mini-ork tree (no db/migrations): {cwd}", file=sys.stderr)
        return 2

    db = os.environ.get("MINI_ORK_DB", "").strip()
    if not db or not os.path.isfile(db):
        print(f"MINI_ORK_DB unset or missing: {db!r}", file=sys.stderr)
        return 2

    for unit in UNITS:
        print(unit)
    for unit, why in _OTHERWISE_UNIT.items():
        # Stderr, not stdout: the contract reads stdout as the unit list, and a
        # note padded into it would be dispatched as a unit id.
        print(f"[miniork-self] not listed — {unit}: {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
