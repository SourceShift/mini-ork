"""``mini-ork settle`` — run a finished run's deferred learning side-channels.

    mini-ork settle <run_id>

The post-run learning side-channels (rubric grading, gradient extraction,
pattern induction, GRPO writeback, OTel export) normally run at the end of
``mini-ork run``. They cost three to five minutes, which is dead time for any
caller that dispatches the next deliverable as soon as the run ends. Dispatch
those runs with ``MO_POST_RUN_LEARNING=0`` and settle afterwards:

    MO_POST_RUN_LEARNING=0 mini-ork run recursive-validate-impl kickoff.md
    mini-ork settle run-1795444993-72699

The channels themselves are fill-only and best-effort, so settling a run twice
is safe: reward rows that already carry a value are left alone.

Exit codes: 0 settled, 2 usage error or unknown run.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

HELP_TEXT = (
    "Usage: mini-ork settle <run_id>\n"
    "\n"
    "Run the deferred post-run learning side-channels for a finished run.\n"
    "Reads MINI_ORK_HOME (default: ./.mini-ork) and MINI_ORK_DB\n"
    "(default: $MINI_ORK_HOME/state.db).\n"
    "\n"
    "  --help   show this help\n"
)

# ``task_runs.verdict`` vocabulary (db/migrations/0013_task_runs.sql) plus the
# looser spellings older rows and recipes write. Only a listed pass word yields
# fail_count 0; anything else non-empty is a failure, which is what the stamp
# wants (it only distinguishes 0 from non-0).
_PASS_VERDICTS = {"pass", "passed", "success", "succeeded", "approved", "approve"}
_PASS_STATUSES = {"published"}
_FAIL_STATUSES = {"failed", "rolled_back"}
# A row still in one of these has not finished; grading it now would grade a
# partial run, so settle says so and leaves the run-verified stamp unwritten.
_LIVE_STATUSES = {"classified", "planned", "executing", "verifying", "reviewing"}


def _engine_home() -> Path:
    """The run home, by the engine's own default: ``$MINI_ORK_HOME``, else
    ``<cwd>/.mini-ork`` (``mini_ork/context.py`` ``db_or_default``).

    ``MINI_ORK_ROOT`` is the repo root, NOT the home — reading it as one pointed
    at ``<repo>/state.db`` and found no database at all.
    """
    env = os.environ.get("MINI_ORK_HOME", "").strip()
    return Path(env).expanduser() if env else Path.cwd() / ".mini-ork"


def _db_path(home: Path) -> Path:
    env = os.environ.get("MINI_ORK_DB", "").strip()
    return Path(env).expanduser() if env else home / "state.db"


def _task_class(run_dir: Path) -> str:
    profile = run_dir / "run_profile.json"
    if not profile.is_file():
        return ""
    try:
        data = json.loads(profile.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("task_class") or "") if isinstance(data, dict) else ""


def _fail_count(verdict: str, status: str) -> int | None:
    """0 / 1 from the run's own bookkeeping, or None when it recorded neither.

    ``None`` leaves the run-verified stamp unwritten (the documented behaviour
    when the verdict is unknown); the other channels still run.
    """
    v = (verdict or "").strip().lower()
    if v:
        return 0 if v in _PASS_VERDICTS else 1
    s = (status or "").strip().lower()
    if s in _PASS_STATUSES:
        return 0
    if s in _FAIL_STATUSES:
        return 1
    return None


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)
    if not rest:
        print(HELP_TEXT, file=sys.stderr)
        return 2
    if rest[0] in {"-h", "--help", "help"}:
        print(HELP_TEXT)
        return 0
    run_id = rest[0]
    home = _engine_home()
    db = _db_path(home)
    if not db.is_file():
        print(f"settle: no state db at {db}", file=sys.stderr)
        return 2

    # ``task_runs`` is the run registry (``id TEXT``: ``run-<ts>-<pid>``). The
    # legacy ``runs`` table is an evaluator artifact table keyed by an INTEGER
    # id and holds at most a handful of benchmark rows — querying it here meant
    # every real run answered "not found". It also has no ``run_dir`` column, so
    # the directory comes from the one sanctioned resolver, which knows the rsi
    # child-home shapes (``<home>/rsi/<candidate>/<step>/home/runs/<id>``).
    con = sqlite3.connect(str(db), timeout=15.0)
    con.execute("PRAGMA busy_timeout = 15000")
    try:
        row = con.execute(
            "select status, verdict from task_runs where id = ?", (run_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        print(f"settle: cannot read {db}: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()
    if row is None:
        print(f"settle: run {run_id} not found in {db} (task_runs)",
              file=sys.stderr)
        return 2

    from mini_ork.orchestrator_thread import run_dir as _resolve_run_dir  # noqa: PLC0415

    status = str(row[0] or "")
    verdict = str(row[1] or "")
    run_dir = _resolve_run_dir(home, run_id)
    if not run_dir.is_dir():
        print(f"settle: run dir {run_dir} is missing; nothing to grade",
              file=sys.stderr)
        return 2

    fail_count = _fail_count(verdict, status)
    if status.strip().lower() in _LIVE_STATUSES:
        print(f"settle: warning: run {run_id} is still {status}; settling a "
              "live run grades a partial trajectory", file=sys.stderr)
    task_class = _task_class(run_dir)

    from mini_ork.cli.execute import _post_run_learning  # noqa: PLC0415

    print(f"[settle] run={run_id} dir={run_dir} "
          f"task_class={task_class or '-'} status={status or '-'} "
          f"verdict={verdict or '-'} fail_count={fail_count}")
    # ``force`` is the whole point: a run deferred its learning by exporting
    # MO_POST_RUN_LEARNING=0, and that variable is still in this process's
    # environment, so the unforced gate would defer again and settle would
    # silently do nothing.
    _post_run_learning(str(db), str(run_dir), run_id, task_class,
                       fail_count=fail_count, force=True)
    print("[settle] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
