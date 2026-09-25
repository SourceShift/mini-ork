#!/usr/bin/env python3
"""units-cmd for the mini-ork-self goal loop: emit the pinned unit inventory.

Contract (recipes/goal-loop/lib/goal_state.py::list_units and
verifiers/goal_check.py): run via shell in MO_GOAL_TARGET_CWD, one unit id per
line, rc!=0 aborts the wave. The list is the WATCH signal; the predicate decides
pass/fail per unit, exactly as list_chapters.sh emits all chapters regardless of
their state. Non-vacuous by construction: if the target tree does not look like
mini-ork, or the live db is unreadable, this exits 2 rather than emitting an
empty (and therefore silently green) list.

WHY THESE FIVE UNITS. mini-ork's real pending/broken backlog was enumerated on
2026-09-25 by asking what a check can actually measure, not by asking what looks
untidy. Each of these is a defect this repo is carrying right now, reproduced by
an executable probe that is RED on the tree as it stands:

  migrate-drift-comment-only     a comment reword in 0038 bumped its checksum,
                                 so `mini-ork update` returns 1 on every db that
                                 applied the pre-edit file and dies before
                                 reaching 0054/0055/0056/0058 — which is why
                                 four migrations have sat unapplied for months.
                                 Its second probe additionally requires the
                                 canonical form to stop collapsing whitespace
                                 INSIDE a string literal, which today lets a
                                 real data edit be re-baselined silently.
  migration-0054-drops-columns   0054's create-copy-drop-rename omits exactly
                                 route_margin and predicted_error. On the live
                                 db (where both already exist) applying it
                                 destroys route_margin — the entire training set
                                 the UCCI map is fit from.
  migration-order-is-reported    applying 0054 AFTER 0057/0059 (the state the
                                 live db is already in) is reported nowhere.
                                 The runner is lexicographic, so an out-of-order
                                 apply is indistinguishable from a normal one
                                 until the rebuilt table has already dropped the
                                 later migrations' columns.
  reflect-step-is-bounded        the reflect child is spawned with NO timeout in
                                 two places. Reflect runs after the verdict is
                                 already final, so a lane that never returns
                                 holds `mini-ork run` open forever — which is
                                 why the campaign launcher carries a shell
                                 watchdog that kills reflect past six minutes.
                                 That is a fix living in a shell script instead
                                 of in the code that spawns the child.
  module-child-engine-pin        a `python -m mini_ork.cli.*` child puts its
                                 working directory at sys.path[0], ahead of the
                                 PYTHONPATH that names the engine. The goal-loop
                                 runs children with cwd set to the repo under
                                 repair, so a mini_ork/ tree in the target
                                 shadows the engine and the child executes the
                                 target's stale copy.

Two further candidates were examined and DELIBERATELY EXCLUDED — a unit that
cannot be made to pass is a permanently red wave, and a unit whose "fix" is not
code is not work:

  task-runs-cost-zero            goal-loop runs report $0.00 in
                                 task_runs.cost_usd. NOT LISTED YET: no probe
                                 has been written for it, and its writer sits in
                                 the same region of execute_handlers.py that
                                 module-child-engine-pin edits. Two children from
                                 one wave racing one file is how a wave produces
                                 a conflict instead of a fix. List it in a later
                                 wave, once the pin has landed.
  route-margin-sparse            only 2 of 53 learned/explore traces carry
                                 route_margin. NOT A DEFECT: the write chain
                                 (routing.py -> trace_store.trace_write) is
                                 intact and tested; the margin is legitimately
                                 None when the pick comes from fetch_global_best
                                 or the EquiRouter override, which is what the
                                 MO_LEARNING_MIN_SAMPLES=3 floor forces for most
                                 slices. The remedy is traffic, not code — it is
                                 task #12/#6, the campaign this loop feeds.

The magnitude is stated plainly, because the campaign this feeds is asked for a
real number and not a flattering one: five units, and four of them RED today.
One wave dispatches one child per red unit, so a wave with work to do costs what
five children cost. Once all five go GREEN the wave has nothing left to fix —
every later wave re-runs the same five probes over an unchanged tree, produces
the same verdict, and the driver's own divergence detector stops the run. Five
units do NOT reach a hundred waves. They produce honest waves while real defects
remain and a self-reported stop reason when they run out, which is the point.

Connection comes from the environment; no secret lives here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Pinned, in dispatch order. All are fixable by a code-fix child in the target
# worktree, and all are measurable without touching the live db.
UNITS = (
    "migrate-drift-comment-only",
    "migration-0054-drops-columns",
    "migration-order-is-reported",
    "reflect-step-is-bounded",
    "module-child-engine-pin",
)
_OTHERWISE_UNIT = {
    "task-runs-cost-zero": "not listed: no probe written yet, and its writer shares a file with module-child-engine-pin",
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
