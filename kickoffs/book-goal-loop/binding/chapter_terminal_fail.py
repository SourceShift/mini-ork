#!/usr/bin/env python3
"""terminal-FAILURE detector for the goal-loop await (MO_GOAL_TERMINAL_FAIL_CMD).

Companion to ``chapter_predicate.py``. The predicate answers "has this chapter
PASSED?"; this script answers the opposite terminal question "has it FAILED for
good?" so the await can stop the instant a deploy is proven bad instead of
burning the whole ``MO_GOAL_APPLY_AWAIT_SECONDS`` window (90 min) on a regen
that will never pass.

Contract (recipes/goal-loop/lib/transforms.py::_await_terminal): invoked as
``python3 chapter_terminal_fail.py <chapter_number>`` (argv, not shell) inside
MO_GOAL_TARGET_CWD. Exit 0 == the chapter has TERMINALLY FAILED / stalled ⇒ stop
awaiting. Any non-zero == NOT terminally failed ⇒ keep waiting. The FIRST stdout
line is captured as the reason.

Terminal signals (any ⇒ exit 0):
  * ``permanently_failed = true`` — researcher's own give-up flag. Always on;
    this is the unambiguous terminal state.
  * ``status`` in the failed set AND ``MO_GOAL_FAIL_ON_STATUS_FAILED=1`` — opt-in,
    because researcher can move a chapter to ``failed`` transiently before a
    retry, so treating ``failed`` as terminal by default would abandon a
    recoverable chapter.
  * STALL: ``status='generating'`` AND ``last_error`` present AND no
    verified-artifact run dir under ``MO_GOAL_RUNS_DIR`` has been touched within
    ``MO_GOAL_STALL_SECONDS`` — the chapter is orphaned mid-generation (worker
    crash / lost book-gen singleton lock) and will never advance. Opt-in
    (requires both env vars set), because the chapter-lifecycle ``updated_at``
    does NOT track per-node progress: a HEALTHY long generation keeps
    ``updated_at`` frozen at its last milestone while the internal DAG marches
    W9 -> W10 -> W11 -> ..., so run-dir freshness is the ONLY reliable "is a
    node actually running right now" signal. book-gen is a serial singleton
    (one chapter at a time), so book-global run-dir freshness == the active
    chapter's freshness.

Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

_FAILED_STATUSES = {"failed", "error", "permanently_failed", "aborted"}


def _q(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "-h", os.environ.get("PGHOST", "REDACTED-INTERNAL-IP"),
            "-p", os.environ.get("PGPORT", "5932"),
            "-U", os.environ.get("PGUSER", "researcher_user"),
            "-d", os.environ.get("PGDATABASE", "researcher_db"),
            "-tA", "-F", "|", "-c", sql,
        ],
        capture_output=True,
        text=True,
    )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _newest_run_age_seconds(runs_dir: str) -> float | None:
    """Age (s) of the most recently modified ``run-*`` dir, or None if none.

    A fresh (small age) value means a verified-artifact node ran recently ⇒ the
    generation is churning, not stalled.
    """
    root = Path(runs_dir)
    if not root.is_dir():
        return None
    newest = 0.0
    for child in root.glob("run-*"):
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest = mtime
    if newest == 0.0:
        return None
    return max(0.0, time.time() - newest)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: chapter_terminal_fail.py <chapter_number>", file=sys.stderr)
        return 2
    chapter = argv[0].strip()
    if not re.fullmatch(r"\d+", chapter):
        print(f"bad chapter id: {chapter!r}", file=sys.stderr)
        return 2
    book = os.environ.get("BOOK_UUID", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", book):
        print(f"BOOK_UUID unset/invalid: {book!r}", file=sys.stderr)
        return 2

    sql = (
        "SELECT status, "
        "committed_complete, "
        "permanently_failed, "
        "left(coalesce(last_error,''), 80) "
        "FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' AND chapter_number={chapter};"
    )
    proc = _q(sql)
    if proc.returncode != 0:
        # A DB error is NOT a terminal-failure signal — stay conservative so a
        # transient psql blip cannot abandon a healthy await.
        print(f"ch{chapter} db-error (not terminal): {proc.stderr.strip()[:80]}")
        return 1

    rows = proc.stdout.strip().splitlines()
    if not rows:
        # No lifecycle row: the predicate treats this as "needs work"; mirror
        # that here — absence is not a terminal FAILURE.
        print(f"ch{chapter} no lifecycle row (not terminal)")
        return 1

    cols = (rows[0].split("|") + [""] * 4)[:4]
    status, committed, permfail, lasterr = cols
    status_l = status.strip().lower()

    if committed == "t":
        # Already done — let the pass-predicate own success; not a failure.
        print(f"ch{chapter} committed (not terminal-fail) status={status}")
        return 1

    if permfail == "t":
        print(f"ch{chapter} TERMINAL: permanently_failed=t status={status} "
              f"err={lasterr}")
        return 0

    fail_on_status = os.environ.get("MO_GOAL_FAIL_ON_STATUS_FAILED", "").strip() == "1"
    if fail_on_status and status_l in _FAILED_STATUSES:
        print(f"ch{chapter} TERMINAL: status={status} (MO_GOAL_FAIL_ON_STATUS_FAILED) "
              f"err={lasterr}")
        return 0

    runs_dir = os.environ.get("MO_GOAL_RUNS_DIR", "").strip()
    stall_s = _int_env("MO_GOAL_STALL_SECONDS", 0)
    if runs_dir and stall_s > 0 and status_l == "generating" and lasterr:
        age = _newest_run_age_seconds(runs_dir)
        if age is not None and age >= stall_s:
            print(f"ch{chapter} TERMINAL: stalled — status=generating, last_error set, "
                  f"newest run dir idle {int(age)}s >= {stall_s}s err={lasterr}")
            return 0
        age_str = "none" if age is None else f"{int(age)}s"
        print(f"ch{chapter} not terminal: generating, run-dir age={age_str} "
              f"(< {stall_s}s ⇒ still churning)")
        return 1

    print(f"ch{chapter} not terminal: status={status} permfail={permfail}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
