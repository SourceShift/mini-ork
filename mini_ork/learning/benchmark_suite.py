"""benchmark_suite — Python port of lib/benchmark_suite.sh.

Faithful port of the four bash entry points: ``benchmark_add``,
``benchmark_list``, ``benchmark_run``, ``benchmark_results``. The bash
script is the authoritative source; this module gives Python callers
an in-process target and gives
``tests/unit/test_benchmark_suite_py.py`` a stable surface to diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Co-existence model (strangler-fig): ``lib/benchmark_suite.sh`` stays
byte-identical. This port mirrors the embedded Python heredoc exactly —
same INSERT column set, same ON CONFLICT update set, same FK bootstrap
(synthesized epic + run row to satisfy ``benchmark_results.run_id
REFERENCES runs(id)``), same subprocess wrapper, same summary key
ordering. Parity is enforced by the sibling test (>=6 live-subprocess
cases; floats within 1e-6, JSON shapes equal, DB row-diff zero on
stable columns).

Schema caveat (closed by the test harness): ``lib/benchmark_suite.sh``'s
private ``_bench_ensure_tables`` builds a STALE DDL (column names
``id``, ``input``, ``expected_artifact_hash_or_criteria``, INTEGER
``created_at``) that DIFFERS from migration 0010's column set. The
bash INSERT itself targets the canonical 0010 columns, so the only
correct bootstrap path is ``db/init.sh`` (which applies migrations
0001, 0010, …) — letting bash's private DDL win would crash the
INSERT with a no-such-column error. The parity test therefore
bootstraps via ``db/init.sh`` with ``MINI_ORK_HOME`` /
``MINI_ORK_DB`` env vars, never the bash-side DDL.

DB resolution: bash uses ``${MINI_ORK_DB:?}`` (errors if unset). The
port raises ValueError when ``db`` is None AND ``MINI_ORK_DB`` is
unset — it never silently reads a cwd-relative default.

WS5 cutover — runner execution:
    The bash ``benchmark_run`` shelled out to
    ``bash -c 'source $MINI_ORK_ROOT/lib/utility_function.sh; <runner_fn>'``
    with the task JSON on stdin. ``runner_fn`` is now native:

    - a **callable** — invoked in-process with the task dict; it may
      return a dict (→ JSON-normalised stdout-equivalent), a JSON/plain
      string (→ stdout-equivalent), or a ``(rc, stdout)`` tuple
      (→ bash rc + stdout semantics). Exceptions map to the bash
      subprocess-error path (``err_msg``, ``passed=False``).
    - the string ``"utility_score"`` — the staged port
      ``mini_ork.learning.utility_function.score`` computes the score on
      the task JSON and the stdout contract (``f"{U:.6f}"``, rc 0;
      rc 1 on invalid JSON) is emulated exactly.
    - any other string — a legacy shell snippet; unsupported natively
      (per-task error, ``passed=False``). Migrate to a callable.

    The 300s subprocess timeout is a bash-only concern and is not
    enforced for in-process runners.

Public surface:
    add(task_json, db=None) -> str           (returns bench_id)
    list_(task_class=None, db=None) -> list[dict]
    run(candidate_id, runner_fn=None, root=None, db=None) -> dict
    results(candidate_id, db=None) -> list[dict]
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

from mini_ork.learning import utility_function as _utility_function


def _resolve_db(db: str | None) -> str:
    resolved = db or os.environ.get("MINI_ORK_DB")
    if not resolved:
        raise ValueError("MINI_ORK_DB unset")
    return resolved


def _resolve_root(root: str | None) -> str:
    resolved = root or os.environ.get("MINI_ORK_ROOT", ".")
    return resolved


def _execute_runner(runner_fn, task: dict) -> tuple[str, bool, str | None]:
    """Execute one benchmark runner natively (WS5).

    Returns ``(run_out, passed, err_msg)`` — the stdout-equivalent
    (stripped, as the bash path's ``proc.stdout.strip()``), the pre-parse
    pass flag (bash: ``rc == 0``), and an error message (None on the
    happy path, mirroring bash's timeout/subprocess-error branches).
    """
    if callable(runner_fn):
        try:
            out = runner_fn(dict(task))
        except Exception as e:
            return "", False, str(e)
        if isinstance(out, tuple) and len(out) == 2:
            rc, stdout = out
            return str(stdout).strip(), int(rc) == 0, None
        if isinstance(out, (dict, list)):
            return json.dumps(out), True, None
        if out is None:
            return "", True, None
        return str(out).strip(), True, None

    if isinstance(runner_fn, str) and runner_fn.strip() == "utility_score":
        # The public API of the sourced lib/utility_function.sh, ported:
        # score the payload the bash subprocess received on stdin and
        # emulate its stdout (f"{U:.6f}") + rc contract.
        try:
            u = _utility_function.score(json.dumps(task))
        except ValueError as e:
            return "", False, str(e)
        return f"{u:.6f}", True, None

    return (
        "",
        False,
        f"runner_fn {runner_fn!r} is a shell snippet; the bash runner was "
        "removed (WS5) — pass a Python callable or 'utility_score'",
    )


def add(task_json: str, db: str | None = None) -> str:
    """Port of ``benchmark_add``. Returns the bench_id on stdout-equivalent."""
    db = _resolve_db(db)
    try:
        t = json.loads(task_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"benchmark_add: invalid JSON: {e}") from e

    bench_id = t.get("benchmark_id") or t.get("id")
    if not bench_id or not t.get("task_class"):
        raise ValueError(
            "benchmark_add: benchmark_id (or id) + task_class required"
        )

    con = sqlite3.connect(db)
    con.execute(
        """
        INSERT INTO benchmark_tasks
            (benchmark_id, task_class, input_payload,
             expected_artifact_hash, expected_criteria,
             success_verifiers, baseline_utility_score, source)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(benchmark_id) DO UPDATE SET
            task_class=excluded.task_class,
            input_payload=excluded.input_payload,
            expected_artifact_hash=excluded.expected_artifact_hash,
            expected_criteria=excluded.expected_criteria,
            success_verifiers=excluded.success_verifiers,
            baseline_utility_score=excluded.baseline_utility_score
        """,
        (
            bench_id,
            t["task_class"],
            json.dumps(t.get("input_payload") or t.get("input") or {}),
            t.get("expected_artifact_hash")
            or t.get("expected_artifact_hash_or_criteria")
            or "",
            json.dumps(t.get("expected_criteria") or {}),
            json.dumps(t.get("success_verifiers", [])),
            float(t.get("baseline_utility_score", 0.0)),
            t.get("source") or "human",
        ),
    )
    con.commit()
    con.close()
    return bench_id


def list_(task_class: str | None = None, db: str | None = None) -> list[dict]:
    """Port of ``benchmark_list``. Returns list of dicts (Python equivalent
    of bash's ``print(json.dumps(...))``)."""
    db = _resolve_db(db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    if task_class:
        rows = con.execute(
            "SELECT * FROM benchmark_tasks WHERE task_class=? ORDER BY benchmark_id",
            (task_class,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM benchmark_tasks ORDER BY task_class, benchmark_id"
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def run(
    candidate_id: str,
    runner_fn: str | None = None,
    root: str | None = None,
    db: str | None = None,
) -> dict:
    """Port of ``benchmark_run``. Returns summary dict (Python equivalent
    of bash's ``print(json.dumps(...))``).

    Mirrors the bash FK bootstrap exactly:
      - INSERT OR IGNORE INTO epics (id='benchmark-'+cid[:16])
      - INSERT INTO runs (epic_id, run_dir='benchmark/'+cid+'/'+uuid6, ...)
        → lastrowid
      - INSERT INTO benchmark_results with the parent run_id
    """
    db = _resolve_db(db)
    _resolve_root(root)  # accepted for bash-signature compat; runner is native now
    now = int(time.time())

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    tasks = con.execute("SELECT * FROM benchmark_tasks").fetchall()

    # FK bootstrap — synthetic epic + runs row.
    benchmark_epic_id = f"benchmark-{candidate_id[:16]}"
    con.execute(
        """
        INSERT INTO epics (id, title, status, notes)
        VALUES (?, ?, 'in progress', 'synthetic — benchmark FK bootstrap')
        ON CONFLICT(id) DO NOTHING
        """,
        (benchmark_epic_id, f"Benchmark run for candidate {candidate_id}"),
    )
    run_id = con.execute(
        """
        INSERT INTO runs (epic_id, run_dir, branch, baseline_sha, agent, final_verdict)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (
            benchmark_epic_id,
            f"benchmark/{candidate_id}/{uuid.uuid4().hex[:8]}",
            "main",
            "benchmark-synthetic",
            "mini-ork",
        ),
    ).lastrowid

    results_list = []
    for task_row in tasks:
        t = dict(task_row)
        tid = t["benchmark_id"]
        result_id = (
            f"br-{candidate_id[:8]}-{tid[:8]}-{uuid.uuid4().hex[:6]}"
        )
        run_out: str | None = None
        passed = False
        util_score = 0.0
        err_msg: str | None = None

        if runner_fn:
            run_out, passed, err_msg = _execute_runner(runner_fn, t)
            if err_msg is None:
                try:
                    data = json.loads(run_out)
                    util_score = float(data.get("utility_score", 0.0))
                    passed = bool(data.get("passed", passed))
                    run_out = json.dumps(data)
                except Exception:
                    util_score = 1.0 if passed else 0.0
        else:
            run_out = json.dumps(
                {"skipped": True, "reason": "no MINI_ORK_WORKFLOW_RUNNER_FN set"}
            )
            util_score = float(t.get("baseline_utility_score", 0.0) or 0.0)
            passed = False
            err_msg = "runner not configured"

        evidence_path = run_out or ""
        con.execute(
            """
            INSERT INTO benchmark_results
                (result_id, benchmark_id, candidate_id, run_id,
                 pass, utility_score, evidence_path)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                result_id,
                tid,
                candidate_id,
                run_id,
                int(bool(passed)),
                util_score,
                evidence_path,
            ),
        )

        results_list.append({
            "benchmark_id": tid,
            "task_class": t["task_class"],
            "passed": passed,
            "utility_score": util_score,
            "error": err_msg,
        })

    con.commit()
    con.close()

    passed_count = sum(1 for r in results_list if r["passed"])
    total = len(results_list)
    avg_util = (
        sum(r["utility_score"] for r in results_list) / total
        if total
        else 0.0
    )

    return {
        "candidate_id": candidate_id,
        "ran_at": now,
        "total_tasks": total,
        "passed": passed_count,
        "failed": total - passed_count,
        "avg_utility_score": round(avg_util, 4),
        "all_pass": passed_count == total,
        "results": results_list,
    }


def results(candidate_id: str, db: str | None = None) -> list[dict]:
    """Port of ``benchmark_results``. Returns list of rows ordered by
    ran_at DESC."""
    db = _resolve_db(db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM benchmark_results WHERE candidate_id=? ORDER BY ran_at DESC",
        (candidate_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]
