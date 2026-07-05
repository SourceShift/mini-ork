"""mini_ork_eval — Python port of ``bin/mini-ork-eval``.

Faithful port of the bash entry point that evaluates a workflow candidate
against the benchmark suite and emits ``utility_delta`` on stdout. Re-uses
``mini_ork.ported.benchmark_suite`` so we don't re-port that surface.

The bash script (bin/mini-ork-eval) is the authoritative source; this
module gives Python callers an in-process target and gives
``tests/unit/test_mini_ork_eval_py.py`` a stable surface to diff
against the LIVE bash subprocess (no mocks, no hardcoded outputs).

Co-existence model (strangler-fig): ``bin/mini-ork-eval`` stays
byte-identical. The python port mirrors the embedded stdout/stderr
block-for-block — same 14-line usage block, same ``=== mini-ork eval ===``
header/footer, same ``tasks_evaluated: 0 / total_utility: 0 /
baseline_utility:0 / utility_delta:   0`` lines, same trailing
``utility_delta=0`` line, same 4-line "Candidate not found" stderr,
same PRAGMA-WAL UPDATE heredoc into ``workflow_candidates``. Parity
is enforced by the sibling test (8 live-subprocess cases, floats
within 1e-6, stdout bytes-equal, DB row-diff zero on stable columns).

Latent bash bug preserved (parity-required): bash's
``_eval_result_cb`` is shell-internal and never crosses the
bash→python boundary. Concretely:
  * ``benchmark_run`` has no flag parsing — it always takes ``$1``
    as ``candidate_id`` and ignores ``--suite``/``--candidate-id``/
    ``--workflow-file``/``--executor``/``--on-result-callback``.
  * ``utility_compute`` / ``utility_baseline`` are called by the
    callback but do not exist in ``lib/utility_function.sh``.
  * The callback never fires, so ``TOTAL_UTILITY`` /
    ``BASELINE_UTILITY`` / ``TASK_COUNT`` are always ``0`` in bash,
    and ``utility_delta`` is always ``0``.

The python port mirrors that literal behaviour — total_utility=0,
baseline_utility=0, task_count=0, utility_delta=0 every non-dry-run.
``benchmark_suite.run`` is still called (to mirror the bash side-effect
of writing a synthetic epic + runs row + benchmark_results rows) but
its return value is discarded because the bash callback that would
have populated TOTAL/BASELINE/TASK_COUNT never fires.

DB resolution: bash reads ``${MINI_ORK_DB:-${MINI_ORK_HOME}/state.db}``
with env > cwd fallback. The port mirrors that exact order — env wins,
then ``$MINI_ORK_HOME/state.db``, then ``cwd/.mini-ork/state.db``.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from mini_ork.ported import benchmark_suite


USAGE = """Usage: mini-ork eval --candidate <id> [--suite <name>] [--dry-run]

Run the benchmark suite against a workflow candidate and compute utility delta
vs the current baseline workflow.

Outputs utility_delta on stdout (positive = improvement).

Options:
  --candidate <id>   Workflow candidate ID (required)
  --suite <name>     Benchmark suite to use (default: "default")
  --dry-run          List benchmark tasks; do not dispatch
  --help             Show this help
"""


def _usage() -> str:
    return USAGE


def _resolve_db_path() -> str:
    """Mirror bash: ``${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}`` with
    env MINI_ORK_DB winning, then ``$MINI_ORK_HOME/state.db``, then
    ``cwd/.mini-ork/state.db`` (the bash fallback path)."""
    db = os.environ.get("MINI_ORK_DB")
    if db:
        return db
    home = os.environ.get("MINI_ORK_HOME")
    if home:
        return os.path.join(home, "state.db")
    return os.path.join(os.getcwd(), ".mini-ork", "state.db")


def _ensure_trace_id() -> str:
    """Mirror bash: ``tr-eval-$(date +%s)-$$``."""
    return f"tr-eval-{int(time.time())}-{os.getpid()}"


def _emit_candidate_not_found(candidate_id: str) -> None:
    """Mirror bash's 4-line stderr block (bin/mini-ork-eval:101-106)."""
    sys.stderr.write(f"Candidate not found in DB: {candidate_id}\n")
    sys.stderr.write(
        "Either the candidate_id is wrong OR its base_workflow_version_id\n"
    )
    sys.stderr.write(
        "doesn't have a matching workflow_memory row (FK gap).\n"
    )
    sys.stderr.write(
        "Run 'mini-ork improve' first to generate candidates with proper baselines.\n"
    )


def _resolve_candidate(db: str, candidate_id: str) -> str:
    """Issue the JOIN ``workflow_candidates → workflow_memory.yaml_blob``
    query. Returns the ``yaml_blob`` for the candidate. Raises
    ``SystemExit(2)`` with the same 4-line stderr message bash emits on miss."""
    if not os.path.isfile(db):
        _emit_candidate_not_found(candidate_id)
        sys.exit(2)
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            """
            SELECT wm.yaml_blob
            FROM workflow_candidates wc
            JOIN workflow_memory wm
              ON wc.base_workflow_version_id = wm.workflow_version_id
            WHERE wc.candidate_id = ?
            LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        _emit_candidate_not_found(candidate_id)
        sys.exit(2)
    return row[0] or ""


def _write_candidate_result(
    db: str,
    candidate_id: str,
    utility_delta: float,
) -> None:
    """Mirror bash's inline python heredoc at bin/mini-ork-eval:170-196.

    Issues ``UPDATE workflow_candidates SET utility_delta=?, status='shadow'
    WHERE candidate_id=?`` via sqlite3 with ``PRAGMA journal_mode=WAL``.
    Wrapped in try/except OperationalError that prints
    ``[warn] DB update skipped: {e}`` to stderr (does NOT raise).
    """
    try:
        con = sqlite3.connect(db)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                """
                UPDATE workflow_candidates
                SET utility_delta = ?,
                    status        = 'shadow'
                WHERE candidate_id = ?
                """,
                (float(utility_delta), candidate_id),
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        sys.stderr.write(f"[warn] DB update skipped: {e}\n")


def _safe_trace_write(payload: str) -> None:
    """Mirror bash's ``trace_write ... >/dev/null 2>&1 || true`` suppression.

    No python port of trace_store exists yet. If a port surfaces later,
    we route through it; otherwise this is a silent no-op.
    """
    try:
        from mini_ork.ported import trace_store  # type: ignore  # noqa: F401
    except ImportError:
        return
    try:
        trace_store.write(payload)  # type: ignore[attr-defined]
    except Exception:
        pass


def _parse_argv(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Mirror bash's arg-parsing exactly. We use ``parse_known_args`` so
    unknown flags surface in ``extras`` rather than argparse's own
    error path — bash emits ``"Unknown flag: X. Try --help"`` and exits
    2; argparse's default error message would diverge."""
    parser = argparse.ArgumentParser(
        prog="mini-ork eval",
        add_help=False,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--candidate", metavar="<id>", default=None)
    parser.add_argument("--suite", metavar="<name>", default="default")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--help", "-h", action="store_true", dest="help")
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    args, extras = _parse_argv(argv)

    if args.help:
        sys.stdout.write(_usage())
        return 0

    if extras:
        sys.stderr.write(f"Unknown flag: {extras[0]}. Try --help\n")
        return 2

    if not args.candidate:
        # bash: [ -z "$CANDIDATE_ID" ] && { _usage; exit 2; }
        # Note: bash writes usage to STDOUT (cat <<EOF), not stderr.
        sys.stdout.write(_usage())
        return 2

    dry_run = bool(args.dry_run) or os.environ.get("MINI_ORK_DRY_RUN") == "1"

    db = _resolve_db_path()
    trace_id = _ensure_trace_id()

    if not dry_run:
        _safe_trace_write(
            json.dumps(
                {
                    "trace_id": trace_id,
                    "task_class": "__eval__",
                    "status": "running",
                }
            )
        )

    candidate_workflow = _resolve_candidate(db, args.candidate)

    # ── header (bash lines 113-116) ──────────────────────────────────────
    sys.stdout.write("=== mini-ork eval ===\n")
    sys.stdout.write(f"    candidate: {args.candidate}\n")
    sys.stdout.write(f"    suite:     {args.suite}\n")
    sys.stdout.write("\n")

    if dry_run:
        # bash lines 118-128: benchmark_list --task-class <suite>;
        # echo "[dry-run] would run each task with candidate workflow=<id>"
        tasks = benchmark_suite.list_(task_class=args.suite, db=db)
        sys.stdout.write(json.dumps(tasks) + "\n")
        sys.stdout.write(
            f"[dry-run] would run each task with candidate workflow={args.candidate}\n"
        )
        return 0

    # ── non-dry-run: mktemp workflow file + dispatch ─────────────────────
    # bash writes $CANDIDATE_WORKFLOW to /tmp/mini-ork-candidate-XXXXXX.yaml
    # even though benchmark_run never reads it (it ignores --workflow-file).
    # Mirror that side-effect for filesystem parity.
    fd, wf_path = tempfile.mkstemp(
        prefix="mini-ork-candidate-", suffix=".yaml", dir="/tmp"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(candidate_workflow)

        # bash calls benchmark_run with weird args (callback never fires,
        # utility_compute/baseline don't exist). TOTAL/BASELINE/TASK_COUNT
        # are hard-coded 0. The python port mirrors that literal behavior.
        # benchmark_suite.run() iterates benchmark_tasks but since
        # runner_fn=None, all tasks are skipped.
        #
        # Note: bash does NOT capture benchmark_run's stdout — the summary
        # JSON it prints via the embedded python heredoc lands directly on
        # our stdout. The port mirrors that side-effect: print the summary
        # dict bash would have printed.
        summary = benchmark_suite.run(
            args.candidate,
            runner_fn=None,
            root=str(Path(__file__).resolve().parents[2]),
            db=db,
        )
        sys.stdout.write(json.dumps(summary) + "\n")

        total_utility = 0
        baseline_utility = 0
        task_count = 0
        utility_delta = 0

        sys.stdout.write("\n")
        sys.stdout.write("=== eval result ===\n")
        sys.stdout.write(f"    candidate:       {args.candidate}\n")
        sys.stdout.write(f"    tasks_evaluated: {task_count}\n")
        sys.stdout.write(f"    total_utility:   {total_utility}\n")
        sys.stdout.write(f"    baseline_utility:{baseline_utility}\n")
        sys.stdout.write(f"    utility_delta:   {utility_delta}\n")
        sys.stdout.write("\n")
        sys.stdout.write(f"utility_delta={utility_delta}\n")

        _write_candidate_result(
            db, args.candidate, float(utility_delta)
        )
    finally:
        try:
            os.remove(wf_path)
        except OSError:
            pass

    _safe_trace_write(
        json.dumps(
            {
                "trace_id": trace_id,
                "task_class": "__eval__",
                "status": "success",
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())