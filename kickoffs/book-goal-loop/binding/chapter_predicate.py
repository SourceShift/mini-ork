#!/usr/bin/env python3
"""predicate-cmd for the goal-loop: is chapter <n> of BOOK_UUID done + good?

Contract (recipes/goal-loop/lib/goal_state.py::evaluate_units): invoked as
``python3 chapter_predicate.py <chapter_number>`` (argv, not shell) inside
MO_GOAL_TARGET_CWD. Exit 0 == the unit passes the goal; any non-zero == the
unit still needs work. The FIRST stdout line is captured as the reason.

Goal predicate (the user's bar — "all chapters written at highest quality"):
    committed_complete = true  AND  rubric_status = 'pass'

``committed_complete`` is researcher's own "this chapter is finished + persisted"
flag; ``rubric_status = 'pass'`` is researcher's own quality gate. Both are
columns on ``book_chapter_lifecycle`` (the live generation-status table), so the
loop trusts researcher's own definition of done rather than re-deriving it.

Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys


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


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: chapter_predicate.py <chapter_number>", file=sys.stderr)
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
        "coalesce(rubric_status,''), "
        "committed_complete, "
        "permanently_failed, "
        "degraded, "
        "generation_attempts, "
        "coalesce(committed_markdown_length, markdown_length, 0), "
        "left(coalesce(last_error,''), 80) "
        "FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' AND chapter_number={chapter};"
    )
    proc = _q(sql)
    if proc.returncode != 0:
        print(f"ch{chapter} db-error: {proc.stderr.strip()[:80]}")
        return 3

    rows = proc.stdout.strip().splitlines()
    if not rows:
        print(f"ch{chapter} missing: no lifecycle row for this chapter")
        return 1

    cols = (rows[0].split("|") + [""] * 8)[:8]
    status, rubric, committed, permfail, degraded, attempts, mdlen, lasterr = cols
    committed_ok = committed == "t"
    rubric_ok = rubric == "pass"

    if committed_ok and rubric_ok:
        print(f"ch{chapter} PASS status={status} rubric={rubric} mdlen={mdlen}")
        return 0

    reason = (
        f"ch{chapter} FAIL status={status} rubric={rubric or 'none'} "
        f"committed={committed} permfail={permfail} degraded={degraded} "
        f"attempts={attempts} mdlen={mdlen}"
    )
    if lasterr:
        reason += f" err={lasterr}"
    print(reason)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
