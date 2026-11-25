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
    "Reads MINI_ORK_HOME (default: the engine root) and MINI_ORK_DB\n"
    "(default: $MINI_ORK_HOME/state.db).\n"
    "\n"
    "  --help   show this help\n"
)

_PASS_VERDICTS = {"pass", "passed", "success", "succeeded", "approved", "approve"}


def _engine_root() -> Path:
    env = os.environ.get("MINI_ORK_HOME") or os.environ.get("MINI_ORK_ROOT")
    return Path(env).expanduser() if env else Path.cwd()


def _db_path(root: Path) -> Path:
    env = os.environ.get("MINI_ORK_DB")
    return Path(env).expanduser() if env else root / "state.db"


def _task_class(run_dir: Path) -> str:
    profile = run_dir / "run_profile.json"
    if not profile.is_file():
        return ""
    try:
        data = json.loads(profile.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("task_class") or "") if isinstance(data, dict) else ""


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)
    if not rest:
        print(HELP_TEXT, file=sys.stderr)
        return 2
    if rest[0] in {"-h", "--help", "help"}:
        print(HELP_TEXT)
        return 0
    run_id = rest[0]
    root = _engine_root()
    db = _db_path(root)
    if not db.is_file():
        print(f"settle: no state db at {db}", file=sys.stderr)
        return 2

    con = sqlite3.connect(str(db), timeout=15.0)
    con.execute("PRAGMA busy_timeout = 15000")
    try:
        row = con.execute(
            "select run_dir, final_verdict from runs where id = ?", (run_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        print(f"settle: cannot read {db}: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()
    if row is None:
        print(f"settle: run {run_id} not found in {db}", file=sys.stderr)
        return 2

    run_dir = Path(row[0]) if row[0] else root / "runs" / run_id
    if not run_dir.is_dir():
        print(f"settle: run dir {run_dir} is missing; nothing to grade",
              file=sys.stderr)
        return 2

    verdict = (row[1] or "").strip().lower()
    # None leaves the run-verified stamp unwritten, which is the documented
    # behaviour when the verdict is unknown; the other channels still run.
    fail_count = None if not verdict else (0 if verdict in _PASS_VERDICTS else 1)
    task_class = _task_class(run_dir)

    from mini_ork.cli.execute import _post_run_learning  # noqa: PLC0415

    print(f"[settle] run={run_id} dir={run_dir} "
          f"task_class={task_class or '-'} fail_count={fail_count}")
    _post_run_learning(str(db), str(run_dir), run_id, task_class,
                       fail_count=fail_count)
    print("[settle] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
