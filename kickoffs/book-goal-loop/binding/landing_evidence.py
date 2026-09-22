#!/usr/bin/env python3
"""Deep evidence for the LANDING goal-loop: measure the pre-writing wedge.

job_thrash.py classify reads the burst ledger — useless for a job that never
started a burst. For the draft-region wedge the diagnostic signal is mostly
ABSENCE: no planning session, no planning events, no queue dispatch, while the
form keeps being saved. This harvester states each absence explicitly, because
"the table has no rows for this job" is exactly the fact the fix child needs
and exactly the fact it cannot see from a one-line predicate reason.

Contract: invoked as ``python3 landing_evidence.py <run_uuid>`` (argv, not
shell) inside MO_GOAL_TARGET_CWD; stdout is the evidence the wave hands the
fix child. Exit 0 even on partial reads — partial evidence beats none — but
every failed probe is reported as such, never silently skipped.
Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys


def _q(sql: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["psql", "-h", os.environ.get("PGHOST", "REDACTED-INTERNAL-IP"),
         "-p", os.environ.get("PGPORT", "5932"),
         "-U", os.environ.get("PGUSER", "researcher_user"),
         "-d", os.environ.get("PGDATABASE", "researcher_db"),
         "-tA", "-F", "|", "-c", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip()[:200]
    return True, proc.stdout.rstrip("\n")


def _section(title: str, ok: bool, body: str) -> None:
    print(f"\n## {title}")
    if not ok:
        print(f"(probe FAILED: {body})")
    elif not body.strip():
        print("(no rows — this absence is itself the signal)")
    else:
        print(body)


def main(argv: list[str]) -> int:
    if not argv or not re.fullmatch(r"[0-9a-fA-F-]{36}", argv[0].strip()):
        print("usage: landing_evidence.py <run_uuid>", file=sys.stderr)
        return 2
    uuid = argv[0].strip()

    print(f"# Landing-wedge evidence for run {uuid}")

    ok, body = _q(
        "SELECT 'job_id='||r.job_id, 'fsm_state='||coalesce(f.current_state,'(none)'), "
        "'fsm_rev='||coalesce(f.state_revision::text,'-'), "
        "'fsm_updated='||coalesce(to_char(f.updated_at,'YYYY-MM-DD HH24:MI'),'-'), "
        "'run_created='||to_char(r.created_at,'YYYY-MM-DD HH24:MI'), "
        "'last_step='||coalesce(r.last_step,'(none)'), "
        "'error_class='||coalesce(r.error_class,'(none)'), "
        "'error_message='||coalesce(left(r.error_message,200),'(none)'), "
        "'hatchet_run='||coalesce(r.hatchet_run_id::text,'(none)'), "
        "'queue_prefix='||coalesce(r.queue_prefix,'(none)'), "
        "'retry_count='||r.retry_count "
        f"FROM book_generation_runs r LEFT JOIN compose_job_fsm_state f ON f.job_id=r.id WHERE r.id='{uuid}';"
    )
    _section("Run row + FSM", ok, body.replace("|", "\n") if ok else body)

    ok, body = _q(
        "SELECT 'session_status='||s.status, 'burst_index='||coalesce(s.burst_index::text,'-'), "
        "'updated='||to_char(s.updated_at,'YYYY-MM-DD HH24:MI') "
        f"FROM compose_planning_sessions s JOIN book_generation_runs r ON r.job_id=s.job_id WHERE r.id='{uuid}';"
    )
    _section("Planning session (compose_planning_sessions)", ok, body)

    ok, body = _q(
        "SELECT e.kind||' x'||count(*)||' (last '||max(to_char(e.created_at,'MM-DD HH24:MI'))||')' "
        f"FROM compose_planning_events e JOIN book_generation_runs r ON r.job_id=e.job_id WHERE r.id='{uuid}' "
        "GROUP BY e.kind ORDER BY count(*) DESC LIMIT 12;"
    )
    _section("Planning events histogram (compose_planning_events)", ok, body)

    ok, body = _q(
        "SELECT a.artifact_kind||' rev='||a.write_revision||' updated='||to_char(a.updated_at,'YYYY-MM-DD HH24:MI') "
        f"FROM book_run_artifacts a JOIN book_generation_runs r ON r.job_id=a.job_id WHERE r.id='{uuid}' "
        "ORDER BY a.updated_at;"
    )
    _section("Run artifacts (book_run_artifacts)", ok, body)

    ok, body = _q(
        "SELECT count(*)::text FROM book_chapter_lifecycle c "
        f"JOIN book_generation_runs r ON r.book_id=c.book_uuid WHERE r.id='{uuid}';"
    )
    _section("Chapter lifecycle rows keyed by run.book_id (identity-trap probe)", ok, body)

    ok, body = _q(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name ~ '(transition|fsm).*(audit|event|history)' OR table_name ~ 'audit.*(transition|fsm)';"
    )
    if ok and body.strip():
        for table in body.strip().splitlines():
            t = table.strip()
            ok2, body2 = _q(
                f"SELECT left(row_to_json(t)::text, 300) FROM {t} t "
                f"WHERE t::text LIKE '%{uuid}%' ORDER BY 1 DESC LIMIT 10;"
            )
            _section(f"Transition audit trail ({t})", ok2, body2)
    else:
        _section("Transition audit trail", ok, body or "(no audit table found)")

    proc = subprocess.run(["ps", "axo", "command"], capture_output=True, text=True)
    workers = [ln for ln in proc.stdout.splitlines()
               if ("runWorker.ts book-generation" in ln
                   or "dev-worker-watchdog.sh book-generation" in ln)]
    _section("book-generation worker process", True,
             "\n".join(workers) if workers
             else "(NO worker/watchdog running — the deploy stage starts one)")

    print("\n## Known context (measured 2026-09-22)")
    print("- UI parked at ?step=plan while FSM sits in `draft`: the `next` "
          "transition (draft->plan) was never applied or never accepted.")
    print("- draft_form_data write_revision keeps rising (autosave works) — "
          "the API and DB path are alive, so the wedge is in the step/FSM "
          "advance path, not connectivity.")
    print("- Golden path to writing: draft -next-> plan -start_planning-> "
          "plan_sketching -sketch_ready-> plan_ready -confirm_plan-> generating "
          "(server/compose/fsm/graphDefinition.json, PROTECTED — fix the code "
          "that fails to traverse it, never the graph).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
