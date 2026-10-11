#!/usr/bin/env python3
"""Planning-wedge detector for the goal-loop on compose (planning) jobs.

The chapter binding watches the WRITE stage: ``book_chapter_lifecycle`` rows,
one chapter per unit, and a predicate over ``committed_complete``. This binding
watches the PLANNING stage, which has no rows to watch — a compose job parked
before plan_ready has no chapters yet, so the chapter loop's units-cmd returns
nothing for it and the loop simply never sees it. That is the hole this closes.

THE WEDGE. A DSP planning burst that cannot satisfy its per-topic evidence
floor (``DSP_EVIDENCE_MIN_SOURCES_PER_TOPIC``, default 2) exhausts its query cap,
seals ``failed_shortfall``, is re-dispatched, and reproduces the IDENTICAL
failure — because burst work is keyed by a stable hash, so a re-plan that
derives the same queries returns the MEMOIZED result rather than re-running it.
Retrying is therefore futile *by construction*, not by bad luck.

WHY THE EXISTING DETECTORS ARE BLIND TO IT. Every shipped sensor keys on
staleness or on per-attempt progress:
  * ``stuckJobHealthChecker`` flags sessions in active/burst_running or absent —
    this burst IS burst_running, so it looks healthy.
  * Step-11 auto-resume requires FSM=failed — the job spends half its cycle in
    ``plan_sketching``, where it never fires.
  * ``execution_progress_revision`` / ``execution_useful_progress_at`` reset per
    attempt, so the progress clock always looks recently-advanced.
The heartbeat is FRESH the entire time (measured: 8808 heartbeats over 2 days
with no progress). The product's own code names this blind spot in
``server/scripts/_drive-resume-1789303550679.ts``: "a wedge the stall detector
cannot see". The only signal that sees it is CROSS-ATTEMPT SIGNATURE
REPETITION: a fresh heartbeat, an unchanging failure reason, and a growing
attempt count. That is what this script measures.

CONTRACT
  ``job_thrash.py list``
      units-cmd shape: one ``book_generation_runs.id`` (uuid) per line, no
      header, rc!=0 aborts the wave. Run inside MO_GOAL_TARGET_CWD.
  ``job_thrash.py classify <run_uuid>``
      exit 0 == WEDGED (a repair target). Exit 1 == not wedged. Exit 2 ==
      usage/DB error. Prints a human diagnosis on stdout, which the launcher
      wires up as MO_GOAL_EVIDENCE_CMD so the fix child sees the real burst
      ledger instead of guessing from a reason string.

Unit identity is the run UUID, because that is what ``compose_job_fsm_state``
and ``book_generation_runs.id`` key on. The text ``job_id``
(``job_1789848841596_5d5f059e``) is what the PLANNING tables key on, and is
resolved by join — never assume the two are interchangeable.

Connection comes from libpq env vars; no secret lives here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Planning-region FSM states. A wedged job cycles plan -> plan_sketching ->
# failed -> plan and spends time in all three, so all three are candidates;
# specificity comes from the burst evidence below, not from the state name
# ('failed' alone is generic — 211 jobs sit there for unrelated reasons).
_PLANNING_STATES = ("plan", "plan_sketching", "failed")

# The failure classes this binding knows how to reason about, matched against
# the leading text of the burst_failed reason. Used only to LABEL the diagnosis
# (the detector itself is class-agnostic — it counts repetition, not identity),
# so a new failure class is still detected; it just gets a generic label.
_KNOWN_CLASSES = {
    "querytotalcapexhausted": "query-cap exhausted: the burst spent every query and sealed failed_shortfall",
    "evidencecoverageshortfallforjob": "evidence coverage shortfall: per-topic floor unmet",
}


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


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _lookback_minutes() -> int:
    return _int_env("MO_GOAL_WEDGE_LOOKBACK_MINUTES", 360)


def _min_failures() -> int:
    return _int_env("MO_GOAL_WEDGE_MIN_FAILURES", 2)


def _min_starts() -> int:
    return _int_env("MO_GOAL_WEDGE_MIN_STARTS", 2)


def _recovery_margin() -> int:
    return _int_env("MO_GOAL_WEDGE_RECOVERY_MARGIN", 2)


def _label(reason: str) -> str:
    """Human label for a failure class, or a generic one for unknown classes.

    Prefix-matched, not exact: only the first 60 chars of a reason are
    normalised, so a long leading phrase can be truncated before the key ends.
    """
    norm = _normalize(reason)
    for key, label in _KNOWN_CLASSES.items():
        if norm.startswith(key):
            return label
    return "repeated planning-burst failure (class not otherwise recognised)"


def _normalize(reason: str) -> str:
    """Collapse a burst_failed reason to its stable, JOB-INDEPENDENT class key.

    Every failure opens with the same English phrase naming the class
    ("Evidence coverage shortfall for ...", "query_total_cap_exhausted: ...")
    and then trails off into volatile detail — the job id, per-topic hit ratios,
    the localised topic labels. Keep the leading phrase; drop the rest.

    Three leaks have to be closed, and each one broke a naive version:
      * The job id sits INSIDE that leading phrase ("... shortfall for
        job_1789848841596_5d5f059e"), and its hex tail contributes real letters
        — "5d5f059e" leaks a spurious "dfe" onto the key. Removed by pattern,
        so two different jobs failing the same way agree.
      * Latin tokens inside the Persian topic labels ("offer", "STAR", "ADHD")
        would leak if we kept more than the leading phrase. Bounding the window
        is not enough on its own — the run has to STOP at the first character
        that is neither a letter nor a space, which is what ``re.match`` does
        here.
      * The cap class is underscored ("query_total_cap_exhausted"), so
        underscores become spaces first; otherwise the run stops at the first
        underscore and every such failure collapses to "query".
    """
    s = re.sub(r"job_\d+_[0-9a-fA-F]+", "job", reason)
    s = s.replace("_", " ")
    match = re.match(r"[A-Za-z ]+", s)
    return (match.group(0) if match else "").replace(" ", "").lower()


def _wedge_sql(extra_where: str = "") -> str:
    """The detector. Emits one row per wedged job, newest failure first.

    Repetition is measured over the LAST TWO failures (not all failures in the
    window): a job legitimately changes failure class as one blocker is fixed
    and the next surfaces — cap-exhausted can give way to shortfall — and
    demanding a single class across the whole window would miss exactly the
    long-lived wedges this exists to catch. What must repeat is the RECENT
    failure: same class, twice, while the burst keeps being re-dispatched.
    """
    lo = _lookback_minutes()
    return f"""
    WITH ev AS (
      SELECT e.job_id AS job_str,
             e.kind,
             e.created_at,
             -- same normalisation as _normalize(): drop the job id, unfold
             -- underscores, keep only the leading letter/space run.
             lower(replace(coalesce((regexp_match(
               regexp_replace(regexp_replace(coalesce(e.payload::json->>'reason',''),
                                             'job_[0-9]+_[0-9a-fA-F]+', 'job', 'g'),
                              '_', ' ', 'g'),
               '^[A-Za-z ]+'))[1], ''), ' ', '')) AS cls
      FROM compose_planning_events e
      WHERE e.created_at > now() - make_interval(mins => {lo})
    ),
    agg AS (
      SELECT job_str,
             count(*) FILTER (WHERE kind='burst_failed')             AS n_failed,
             count(*) FILTER (WHERE kind='burst_started')            AS n_started,
             max(created_at) FILTER (WHERE kind='burst_failed')      AS last_failed_at,
             max(created_at) FILTER (WHERE kind='burst_completed')   AS last_completed_at
      FROM ev GROUP BY job_str
    ),
    last2 AS (
      SELECT job_str, min(cls) AS c1, max(cls) AS c2, count(*) AS n
      FROM (
        SELECT job_str, cls,
               row_number() OVER (PARTITION BY job_str ORDER BY created_at DESC) AS rn
        FROM ev WHERE kind='burst_failed'
      ) t WHERE rn <= 2
      GROUP BY job_str
    ),
    -- Recovery guard. A repair that works does not clear this detector by
    -- changing the FSM instantly; it clears it by making the NEXT attempt not
    -- die where the others did. Every prior attempt died at a bounded lifetime
    -- (start -> failure). So: if the newest attempt started AFTER the last
    -- failure and has already outlived the previous attempts by
    -- MO_GOAL_WEDGE_RECOVERY_MARGIN, it is behaving differently from the wedge
    -- and must not be re-repaired mid-flight. Without this, a job that has just
    -- been fixed reads as still-wedged for as long as its (now healthy) burst
    -- runs, and the loop would dispatch a redundant child at it.
    attempts AS (
      SELECT a.job_str,
             a.last_failed_at,
             (SELECT max(s.created_at) FROM ev s
               WHERE s.job_str = a.job_str AND s.kind='burst_started'
                 AND s.created_at < a.last_failed_at) AS prev_start_at,
             (SELECT max(s.created_at) FROM ev s
               WHERE s.job_str = a.job_str AND s.kind='burst_started'
                 AND s.created_at > a.last_failed_at) AS next_start_at
      FROM agg a
    )
    SELECT r.id::text, a.n_failed, a.n_started,
           to_char(a.last_failed_at, 'MM-DD HH24:MI')
    FROM book_generation_runs r
    JOIN compose_job_fsm_state f ON f.job_id = r.id
    JOIN agg      a ON a.job_str = r.job_id
    JOIN last2    l ON l.job_str = r.job_id
    JOIN attempts p ON p.job_str = r.job_id
    WHERE f.current_state IN ('{"','".join(_PLANNING_STATES)}')
      AND a.n_failed >= {_min_failures()}
      AND a.n_started >= {_min_starts()}
      -- the recent failure repeated: same class twice
      AND l.c1 = l.c2
      -- and nothing has SUCCEEDED since: a completed burst means it advanced
      AND (a.last_completed_at IS NULL OR a.last_completed_at <= a.last_failed_at)
      -- ...and the in-flight attempt has NOT yet outlived its predecessors
      AND NOT (
        p.next_start_at IS NOT NULL
        AND p.prev_start_at IS NOT NULL
        AND (now() - p.next_start_at)
            > {_recovery_margin()} * (p.last_failed_at - p.prev_start_at)
      )
      {extra_where}
    ORDER BY a.last_failed_at DESC NULLS LAST;
    """


def _resolve(uuid: str) -> tuple[str | None, str | None]:
    """run uuid -> (text job_id, current fsm state)."""
    proc = _q(
        "SELECT r.job_id, coalesce(f.current_state,''), r.last_step, "
        "coalesce(r.retry_count,0), "
        "coalesce(to_char(r.started_at,'MM-DD HH24:MI'),''), "
        "coalesce(to_char(r.updated_at,'MM-DD HH24:MI'),'') "
        "FROM book_generation_runs r "
        "LEFT JOIN compose_job_fsm_state f ON f.job_id = r.id "
        f"WHERE r.id='{uuid}';"
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, None
    cols = (proc.stdout.strip().splitlines()[0].split("|") + [""] * 6)[:6]
    return cols[0], "|".join(cols[1:])


def _is_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{36}", value))


def _planning_region_count() -> int | None:
    """How many jobs the detector can SEE in the planning region right now.

    The sensor-integrity guard, and the reason an empty unit list can be
    trusted. ``goal_check`` scores a wave on its units list, so silent-empty is
    the dangerous failure mode: a wrong DB, a wrong job_id namespace, or a
    broken join all return zero rows and look exactly like a healthy board.
    There are always dozens of jobs parked in ``failed``/``plan_sketching``
    (measured: ~226 on 2026-09-21), so zero visible means the detector is blind,
    not that the board is clean. Returning None on a query error keeps that
    case distinct from a genuine zero.
    """
    proc = _q(
        "SELECT count(*) FROM book_generation_runs r "
        "JOIN compose_job_fsm_state f ON f.job_id = r.id "
        "WHERE f.current_state IN "
        "('plan','plan_sketching','failed');"
    )
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip().splitlines()
    try:
        return int(line[0].strip()) if line else None
    except ValueError:
        return None


def cmd_list() -> int:
    visible = _planning_region_count()
    if visible is None:
        print("job_thrash: cannot count the planning region (db error) — refusing "
              "to report a clean board from a failing query", file=sys.stderr)
        return 2
    if visible == 0:
        print("job_thrash: 0 jobs visible in the planning region — the detector "
              "is blind (wrong db / job_id namespace), not the board clean",
              file=sys.stderr)
        return 2
    proc = _q(_wedge_sql())
    if proc.returncode != 0:
        print(f"job_thrash: db error: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return 2
    for line in proc.stdout.strip().splitlines():
        if line.strip():
            print(line.split("|", 1)[0].strip())
    return 0


def cmd_classify(uuid: str) -> int:
    if not _is_uuid(uuid):
        print(f"job_thrash: bad run uuid: {uuid!r}", file=sys.stderr)
        return 2
    proc = _q(_wedge_sql(extra_where=f"AND r.id='{uuid}'"))
    if proc.returncode != 0:
        print(f"job_thrash: db error: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return 2
    if not proc.stdout.strip():
        print(f"{uuid}: not wedged (no repeated identical recent planning failure)")
        return 1

    row = (proc.stdout.strip().splitlines()[0].split("|") + [""] * 4)[:4]
    job_str, detail = _resolve(uuid)

    # Evidence: the last few failures verbatim, the live burst snapshot, and the
    # cap/floor the burst is measured against. This is what the fix child reads.
    reasons = _q(
        "SELECT to_char(created_at,'MM-DD HH24:MI'), "
        "left(regexp_replace(coalesce(payload::json->>'reason',''),'\\s+',' ','g'), 400) "
        f"FROM compose_planning_events WHERE job_id='{job_str}' AND kind='burst_failed' "
        "ORDER BY created_at DESC LIMIT 4;"
    )
    hb = _q(
        "SELECT payload::text FROM compose_planning_events "
        f"WHERE job_id='{job_str}' AND kind='burst_heartbeat' "
        "ORDER BY created_at DESC LIMIT 1;"
    )
    hist = _q(
        "SELECT kind, count(*) FROM compose_planning_events "
        f"WHERE job_id='{job_str}' GROUP BY kind ORDER BY 2 DESC LIMIT 10;"
    )
    sess = _q(
        "SELECT status, burst_index, "
        "coalesce(to_char(execution_heartbeat_at,'MM-DD HH24:MI'),'') "
        "FROM compose_planning_sessions "
        f"WHERE job_id='{job_str}' ORDER BY updated_at DESC LIMIT 1;"
    )

    lines = [
        "# Planning wedge detected",
        "",
        f"run_uuid:    {uuid}",
        f"job_id:      {job_str}",
        f"fsm:         {detail.split('|')[0] if detail else '?'}",
        f"failures/started in the last {_lookback_minutes()}min: "
        f"{row[1]} / {row[2]}   (last failure {row[3]})",
        "session:     " + (sess.stdout.strip().replace("|", "  ") or "none"),
        "",
        "## Why this is a wedge and not a slow run",
        "",
        "The burst heartbeat is FRESH and the failure is IDENTICAL each attempt.",
        "Retrying cannot help: burst work is keyed by a stable hash, so a re-plan",
        "that derives the same queries returns the MEMOIZED result instead of",
        "re-running it. Proof is the repeated reason below, not a guess.",
        "",
    ]
    if reasons.returncode == 0 and reasons.stdout.strip():
        lines.append("## Recent burst_failed reasons (identical class = the wedge)")
        lines.append("")
        for r in reasons.stdout.strip().splitlines():
            ts, _, reason = r.partition("|")
            lines.append(f"- {ts}  {reason}")
            lines.append(f"  class: {_label(reason)}")
        lines.append("")
    if hist.returncode == 0 and hist.stdout.strip():
        lines.append("## Event histogram (all time)")
        lines.append("")
        for h in hist.stdout.strip().splitlines():
            kind, _, n = h.partition("|")
            lines.append(f"- {kind}: {n}")
        lines.append("")
    if hb.returncode == 0 and hb.stdout.strip():
        lines.append("## Live burst snapshot (the fresh heartbeat)")
        lines.append("")
        lines.append(hb.stdout.strip())
        lines.append("")
    lines.append("## Suspected code path")
    lines.append("")
    lines.append("Query scope is chosen in server/compose/planningSessionService.ts")
    lines.append("(buildTopicScopedEvidenceQueries / the englishTarget resolution). A")
    lines.append("non-Latin topic label can fail an anchored Latin-script test, resolve")
    lines.append("englishTarget to '', and hand EVERY topic the same global seed list —")
    lines.append("a short list that cannot reach the per-topic floor.")
    lines.append("")
    lines.append("The FLOOR and the QUERY CAP are the bar, and are FROZEN for this loop")
    lines.append("(MO_GOAL_PROTECTED_PATHS includes")
    lines.append("server/services/bookGeneration/evidenceBank/evidenceRetrievalPolicy.ts).")
    lines.append("Fix the query construction; do not lower the bar.")
    print("\n".join(lines))
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: job_thrash.py list | classify <run_uuid>", file=sys.stderr)
        return 2
    sub = argv[0]
    if sub == "list":
        return cmd_list()
    if sub == "classify":
        if len(argv) < 2:
            print("usage: job_thrash.py classify <run_uuid>", file=sys.stderr)
            return 2
        return cmd_classify(argv[1].strip())
    print(f"job_thrash: unknown subcommand {sub!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
