#!/usr/bin/env python3
"""terminal-FAIL detector for the goal-loop await on compose (planning) jobs.

The planning-phase twin of ``chapter_terminal_fail.py``. ``job_predicate.py``
answers "has the job advanced?"; this script answers the opposite question "has
the repair ALREADY FAILED?" so a deploy that does not work costs one poll
instead of the whole ``MO_GOAL_APPLY_AWAIT_SECONDS`` window (90 min). Without
it, a fix that changes nothing sits green-adjacent for an hour and a half while
the burst happily thrashes on in exactly the old way.

Contract (recipes/goal-loop/lib/transforms.py::_await_terminal): invoked as
``python3 job_terminal_fail.py <run_uuid>`` (argv, not shell) inside
MO_GOAL_TARGET_CWD. Exit 0 == TERMINALLY FAILED ⇒ stop awaiting. Any non-zero ==
keep waiting. The FIRST stdout line is captured as the reason.

THE COMPARISON THAT MATTERS. "Did it fail again?" is useless on its own — a
wedged job fails continuously, so that test fires the instant the await starts
and would abandon every repair before it had a chance. What is diagnostic is
whether it failed AGAIN *and IDENTICALLY*: same failure class, in a burst that
started after we deployed. So the first call LATCHES the newest failure (time +
class) into a state file, and only a NEWER failure of the SAME class is
terminal. A newer failure of a DIFFERENT class is progress, not failure — the
fix moved the problem, and the loop should keep watching rather than give up.

That latch is why this script is stateful, and why the state file is keyed by
run uuid: one await per unit, and the baseline must be the state of the world
when THIS await started, not when the loop first ran.

A DB error is NOT a terminal signal (stay conservative), and neither is a
missing latch. Only ``cancelled``/``paused`` short-circuit unconditionally: the
unit is gone, there is nothing left to await.

Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# States that mean "nothing left to await" regardless of failure history.
_DEAD_STATES = {"cancelled", "paused"}


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


def _normalize(reason: str) -> str:
    """Same class key as job_thrash.py: the leading English phrase, job-independent.

    Kept byte-identical on purpose — this script's whole job is to compare a
    failure against the one the detector classified, so the two MUST agree on
    what counts as "the same failure". See job_thrash._normalize for why the
    job id is removed first and why the run stops at the first non-letter.
    """
    s = re.sub(r"job_\d+_[0-9a-fA-F]+", "job", reason)
    s = s.replace("_", " ")
    match = re.match(r"[A-Za-z ]+", s)
    return (match.group(0) if match else "").replace(" ", "").lower()


def _latch_path(uuid: str) -> Path:
    run_dir = os.environ.get("MO_GOAL_RUN_DIR", "").strip()
    base = Path(run_dir) if run_dir else Path(tempfile.gettempdir())
    return base / f".job_wedge_seen_{uuid}"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: job_terminal_fail.py <run_uuid>", file=sys.stderr)
        return 2
    uuid = argv[0].strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        print(f"bad run uuid: {uuid!r}", file=sys.stderr)
        return 2

    state_proc = _q(
        "SELECT coalesce(f.current_state,'') "
        "FROM book_generation_runs r "
        "LEFT JOIN compose_job_fsm_state f ON f.job_id = r.id "
        f"WHERE r.id='{uuid}';"
    )
    if state_proc.returncode != 0:
        print(f"{uuid[:8]} db-error (not terminal): {state_proc.stderr.strip()[:80]}")
        return 1
    state = state_proc.stdout.strip().split("|")[0].strip().lower()
    if state in _DEAD_STATES:
        print(f"{uuid[:8]} TERMINAL: the unit is gone (state={state}) — nothing to await")
        return 0

    fail_proc = _q(
        "SELECT coalesce(extract(epoch from max(e.created_at))::bigint, 0), "
        "coalesce((SELECT coalesce(payload::json->>'reason','') "
        "          FROM compose_planning_events "
        "          WHERE job_id=r.job_id AND kind='burst_failed' "
        "          ORDER BY created_at DESC LIMIT 1), '') "
        "FROM book_generation_runs r "
        "LEFT JOIN compose_planning_events e "
        "  ON e.job_id=r.job_id AND e.kind='burst_failed' "
        f"WHERE r.id='{uuid}' GROUP BY r.job_id;"
    )
    if fail_proc.returncode != 0 or not fail_proc.stdout.strip():
        print(f"{uuid[:8]} no failure history (not terminal)")
        return 1

    cols = (fail_proc.stdout.strip().splitlines()[0].split("|") + ["0", ""])[:2]
    try:
        newest_epoch, reason = int(cols[0]), cols[1]
    except ValueError:
        print(f"{uuid[:8]} unparseable failure row (not terminal)")
        return 1

    if newest_epoch == 0:
        print(f"{uuid[:8]} no burst_failed on record (not terminal)")
        return 1

    latch = _latch_path(uuid)
    cls = _normalize(reason)
    try:
        latched_epoch, latched_cls = latch.read_text().split("\n")[:2]
        latched_epoch, latched_cls = int(latched_epoch), latched_cls
    except (OSError, ValueError):
        # First call of this await: this failure IS the baseline. Writing it and
        # returning "not terminal" is what keeps a wedged job from being declared
        # dead before the repair has had any chance to run.
        try:
            latch.parent.mkdir(parents=True, exist_ok=True)
            latch.write_text(f"{newest_epoch}\n{cls}\n")
        except OSError:
            pass
        print(f"{uuid[:8]} baseline latched: newest failure is class={cls or '(empty)'} "
              "— not terminal yet")
        return 1

    if newest_epoch <= latched_epoch:
        print(f"{uuid[:8]} not terminal: no NEW failure since the deploy "
              f"(latched class={latched_cls or '(empty)'})")
        return 1

    if cls == latched_cls:
        print(f"{uuid[:8]} TERMINAL: failed AGAIN with the SAME class after the "
              f"deploy — class={cls or '(empty)'}. The fix did not change the failure.")
        return 0

    print(f"{uuid[:8]} not terminal: a NEW failure class appeared after the deploy "
          f"({latched_cls or '(empty)'} -> {cls or '(empty)'}) — the fix moved the "
          "problem; keep watching")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
