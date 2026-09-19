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

Stale-projection repair (MO_GOAL_RUBRIC_EVIDENCE, default on). The lifecycle
column can lag the authoritative verdict: a chapter commits, the native chapter
G-Eval pass is persisted to ``book_rubric_results`` against the exact committed
bytes, and ``rubric_status`` is left at 'pending'. Without this the predicate
never flips, and the loop re-dispatches a chapter that is already done — measured
at 223 such rows across the corpus on 2026-09-19, and it cost the ch1 wave six
sweeps. When the committed ``content_hash`` has a matching ``scope='chapter'``
row with ``judge_geval_passed IS TRUE``, that row IS the verdict; the predicate
accepts it and projects it back onto the column. The gate is byte-exact, so it
cannot manufacture a pass for content the judge never saw, and a chapter that
never passed has no such row and still FAILs.

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


def _evidence_enabled() -> bool:
    """Default ON; ``MO_GOAL_RUBRIC_EVIDENCE=0`` restores the strict column bar."""
    return os.environ.get("MO_GOAL_RUBRIC_EVIDENCE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _evidence_gate(chapter: str) -> str:
    """SQL predicate: a native chapter G-Eval pass exists on the committed bytes.

    ``judge_geval_passed`` is researcher's own materialised decision — it is only
    ever set to TRUE when the persist path computed ``rubric_status='pass'`` — so
    matching it against the committed ``content_hash`` re-derives nothing and
    cannot loosen the bar. ``scope='chapter'`` keeps plan-scope rows out, and the
    hash equality keeps a verdict for older bytes from counting for new ones.
    """
    return (
        "EXISTS (SELECT 1 FROM book_rubric_results r "
        f"WHERE r.chapter_number = {chapter} "
        "AND r.scope = 'chapter' "
        "AND r.judge_geval_passed IS TRUE "
        "AND r.evaluated_content_hash = l.content_hash) "
        "AND l.content_hash IS NOT NULL"
    )


def _heal_rubric_status(book: str, chapter: str) -> bool:
    """Project a proven pass onto the lifecycle column. Idempotent.

    Only flips rows that are committed AND still short of 'pass'; a row already
    at 'pass' is left alone, so the repeated poll of ``_await_terminal`` costs
    one no-op UPDATE at most. Returns True when the column now reads 'pass'.
    """
    sql = (
        "UPDATE book_chapter_lifecycle l SET rubric_status = 'pass', "
        "updated_at = NOW() "
        f"WHERE l.book_uuid = '{book}' AND l.chapter_number = {chapter} "
        "AND l.committed_complete IS TRUE "
        "AND l.rubric_status IS DISTINCT FROM 'pass' "
        f"AND {_evidence_gate(chapter)} "
        "RETURNING l.chapter_number;"
    )
    proc = _q(sql)
    return proc.returncode == 0 and proc.stdout.strip() != ""


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

    # The column lags the verdict: a committed chapter whose native G-Eval pass
    # is already persisted has no reason to be dispatched again. Fail-soft — any
    # error here leaves `rubric_ok` false and the chapter FAILs as before.
    healed = False
    if committed_ok and not rubric_ok and _evidence_enabled():
        try:
            healed = _heal_rubric_status(book, chapter)
        except Exception as exc:  # noqa: BLE001 — never let repair break the check
            print(f"ch{chapter} rubric-evidence probe error: {exc!r}", file=sys.stderr)
        if healed:
            rubric = "pass"
            rubric_ok = True

    if committed_ok and rubric_ok:
        note = " rubric-healed=true" if healed else ""
        print(f"ch{chapter} PASS status={status} rubric={rubric} mdlen={mdlen}{note}")
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
