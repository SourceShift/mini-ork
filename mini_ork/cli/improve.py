"""Python port of ``bin/mini-ork-improve`` — workflow evolution dispatcher.

Strangler-fig co-tenant: bash ``bin/mini-ork-improve`` stays untouched; this
port gives Python callers an in-process target and tests a stable surface
for parity verification against the live bash.

Public API:
    parse_args(argv)       # bash case-statement 1:1 mirror
    read_perf_summary(...) # bash SELECT lines 83-95
    print_dry_run(...)     # bash lines 99-104
    improve(...)           # main flow → rc
    main(argv)             # CLI entrypoint

Bash trace_write side effects (1 start + 1 success row per non-dry-run
invocation, both with task_class='__improve__' and the SAME trace_id,
yielding a single UPSERT-ed row with the final status 'success') are
inlined as ``_trace_record_write`` here — no
``mini_ork/ported/trace_store.py`` peer exists yet (kickoff scope is one
file).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Optional


_USAGE = """\
Usage: mini-ork improve [--task-class <name>] [--limit <n>] [--dry-run]

Read recent group performance history and propose WorkflowCandidates for
evaluation and potential promotion.

Outputs candidate IDs on stdout (one per line) for use with:
  mini-ork eval   --candidate <id>
  mini-ork promote --candidate <id>

Options:
  --task-class <name>   Scope to one task class (default: all classes)
  --limit <n>           Max candidates to generate (default: 3)
  --dry-run             Show performance summary; do not generate proposals
  --help                Show this help
"""


def _usage() -> str:
    return _USAGE


def parse_args(argv):
    """Mirror ``bin/mini-ork-improve`` case-statement 1:1.

    Returns dict with keys:
        task_class_filter, candidate_limit, dry_run, help, rc, err
    ``rc`` is non-zero iff bash would exit non-zero (no-args is a happy run).
    """
    out = {
        "task_class_filter": "",
        "candidate_limit": 3,
        "dry_run": os.environ.get("MINI_ORK_DRY_RUN", "0") == "1",
        "help": False,
        "rc": 0,
        "err": "",
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            out["help"] = True
            return out
        if a == "--dry-run":
            out["dry_run"] = True
            i += 1
            continue
        if a == "--task-class":
            if i + 1 >= len(argv):
                out["rc"] = 2
                out["err"] = "--task-class requires a value"
                return out
            out["task_class_filter"] = argv[i + 1]
            i += 2
            continue
        if a == "--limit":
            if i + 1 >= len(argv):
                out["rc"] = 2
                out["err"] = "--limit requires a number"
                return out
            try:
                out["candidate_limit"] = int(argv[i + 1])
            except ValueError:
                out["rc"] = 2
                out["err"] = f"--limit requires a number, got '{argv[i+1]}'"
                return out
            i += 2
            continue
        if a.startswith("-"):
            out["rc"] = 2
            out["err"] = f"Unknown flag: {a}. Try --help"
            return out
        out["rc"] = 2
        out["err"] = f"Unexpected argument: {a}. Try --help"
        return out
    return out


def read_perf_summary(db_path: Optional[str],
                      task_class_filter: str = "") -> list:
    """Mirror bash SELECT lines 83-95 verbatim.

    Returns list of dicts in GROUP-BY column-declaration order
    (task_class, total_runs, successes, avg_duration_ms, avg_cost_usd).

    Returns ``[]`` when the db file is missing OR sqlite3 raises
    (bash falls back to ``echo "[]"`` via ``2>/dev/null || echo "[]"``).
    Coerces COUNT/SUM to ``int`` and AVG to ``float`` so the JSON shape
    matches bash's ``sqlite3 -json`` output (integers stay integers,
    floats stay floats).
    """
    if not db_path or not os.path.isfile(db_path):
        return []
    sql = (
        "SELECT "
        "task_class, "
        "COUNT(*) AS total_runs, "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes, "
        "AVG(CAST(duration_ms AS REAL)) AS avg_duration_ms, "
        "AVG(CAST(cost_usd AS REAL)) AS avg_cost_usd "
        "FROM execution_traces"
    )
    params: list = []
    if task_class_filter:
        sql += " WHERE task_class = ?"
        params.append(task_class_filter)
    sql += " GROUP BY task_class ORDER BY total_runs DESC LIMIT 20"
    try:
        con = sqlite3.connect(db_path)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            rows = con.execute(sql, params).fetchall()
        finally:
            con.close()
    except (sqlite3.OperationalError, FileNotFoundError):
        return []
    out: list = []
    for r in rows:
        out.append({
            "task_class": r[0],
            "total_runs": int(r[1]),
            "successes": int(r[2]),
            "avg_duration_ms": float(r[3]) if r[3] is not None else None,
            "avg_cost_usd": float(r[4]) if r[4] is not None else None,
        })
    return out


def print_dry_run(perf_summary: list, candidate_limit: int,
                  task_class_filter: str) -> None:
    """Mirror bash dry-run output lines 99-104.

    Emits:
        [dry-run] performance summary:
        <pretty-printed JSON of perf_summary, or '' when empty/missing-db>
        <blank separator>
        [dry-run] would call group_evolver with limit=<N>
        [dry-run] scope: task_class=<X>   # only when filter is set
    """
    sys.stdout.write("[dry-run] performance summary:\n")
    if perf_summary:
        # bash pipes through ``python3 -m json.tool`` (indent=4,
        # separators=(', ', ': '), sort_keys=False). Use those kwargs so
        # the framing text is byte-equivalent to bash output.
        sys.stdout.write(json.dumps(
            perf_summary,
            indent=4,
            separators=(", ", ": "),
        ))
        sys.stdout.write("\n")
    else:
        # bash falls into ``|| echo ""`` on json.tool failure → blank line.
        sys.stdout.write("\n")
    sys.stdout.write("\n")
    sys.stdout.write(
        f"[dry-run] would call group_evolver with limit={candidate_limit}\n"
    )
    if task_class_filter:
        sys.stdout.write(
            f"[dry-run] scope: task_class={task_class_filter}\n"
        )


def _trace_record_write(db_path: Optional[str], payload: dict) -> None:
    """Mirror trace_write for the fields bash actually writes for this caller.

    bash emits ``trace_write {"trace_id","task_class","status"}`` once at
    start and once at end with the SAME trace_id; the 2nd call uses
    ``INSERT ... ON CONFLICT(trace_id) DO UPDATE SET status=...`` so the
    end-state is a single row with status='success'. Mirror exactly:
    """
    if not db_path:
        return
    try:
        con = sqlite3.connect(db_path)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute(
                "INSERT INTO execution_traces "
                "(trace_id, task_class, status, created_at) "
                "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(trace_id) DO UPDATE SET status=excluded.status",
                (payload["trace_id"], payload["task_class"], payload["status"]),
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        # bash: ``> /dev/null 2>&1 || true`` swallows all errors silently.
        pass


def improve(*, task_class_filter: str = "", candidate_limit: int = 3,
            dry_run: bool = False, db_path: Optional[str] = None,
            mini_ork_root: Optional[str] = None) -> int:
    """Mirror bin/mini-ork-improve main flow end-to-end.

    Writes stdout/stderr in-process to mirror bash so the parity test can
    compare against captured subprocess output.
    """
    db_path = db_path or os.environ.get("MINI_ORK_DB")
    mini_ork_root = mini_ork_root or os.environ.get("MINI_ORK_ROOT")
    if not mini_ork_root:
        mini_ork_root = os.getcwd()

    trace_id = f"tr-improve-{int(time.time())}-{os.getpid()}"

    if not dry_run:
        _trace_record_write(
            db_path,
            {"trace_id": trace_id,
             "task_class": "__improve__", "status": "running"},
        )

    perf_summary = read_perf_summary(db_path, task_class_filter)

    if dry_run:
        print_dry_run(perf_summary, candidate_limit, task_class_filter)
        return 0

    sys.stdout.write("=== mini-ork improve ===\n")
    sys.stdout.write(
        f"    scope:    {task_class_filter if task_class_filter else 'all'}\n"
    )
    sys.stdout.write(f"    limit:    {candidate_limit}\n")
    sys.stdout.write("\n")

    os.environ["MINI_ORK_GROUP_CANDIDATES"] = str(candidate_limit)

    # Local imports keep module import-time side effects minimal — only
    # the live CLI path pulls group_evolver / workflow_lifecycle in.
    from mini_ork.learning import group_evolver
    from mini_ork.orchestration import workflow_lifecycle

    candidates = group_evolver.propose(
        perf_summary, n_candidates=candidate_limit)

    if not candidates:
        sys.stdout.write(
            "improve: group_evolver produced no candidates "
            "(system may already be near-optimal)\n"
        )
        _trace_record_write(
            db_path,
            {"trace_id": trace_id,
             "task_class": "__improve__", "status": "success"},
        )
        return 0

    sys.stdout.write("Proposed candidates:\n")
    stored_ids: list[str] = []
    for cand in candidates:
        try:
            cid = workflow_lifecycle.candidate_store(
                cand, db=db_path, root=mini_ork_root,
            )
        except (FileNotFoundError, ValueError) as exc:
            # Match bash: emit the friendly message to stderr, then forward
            # the first 3 lines of the actual store stderr/exception text.
            sys.stderr.write("  ✗ failed to store candidate (see stderr)\n")
            for ln in str(exc).split("\n")[:3]:
                sys.stderr.write(f"{ln}\n")
            continue
        sys.stdout.write(f"  candidate_id={cid}\n")
        stored_ids.append(cid)

    sys.stdout.write("\n")
    sys.stdout.write(
        f"Persisted {len(stored_ids)} candidate(s) to "
        "workflow_candidates table.\n"
    )
    sys.stdout.write(
        "Next: mini-ork eval --candidate <id>  "
        "(then: mini-ork promote --candidate <id>)\n"
    )
    for cid in stored_ids:
        sys.stdout.write(f"{cid}\n")

    _trace_record_write(
        db_path,
        {"trace_id": trace_id,
         "task_class": "__improve__", "status": "success"},
    )
    return 0


def main(argv: Optional[list] = None) -> int:
    """CLI entrypoint — mirrors bin/mini-ork-improve end-to-end."""
    if argv is None:
        argv = sys.argv[1:]
    parsed = parse_args(list(argv))

    if parsed["help"]:
        sys.stdout.write(_usage())
        return 0

    if parsed["rc"] != 0:
        # Mirror bash: error to stderr, rc=2.
        sys.stderr.write(f"{parsed['err']}\n")
        return parsed["rc"]

    return improve(
        task_class_filter=parsed["task_class_filter"],
        candidate_limit=parsed["candidate_limit"],
        dry_run=parsed["dry_run"],
    )


if __name__ == "__main__":
    sys.exit(main())
