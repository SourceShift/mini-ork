"""``mini-ork collapse-check`` — report whether a promotion history is collapsing.

Two report-only modes, both of which **always exit 0** — this command reports;
it does not halt anything (wiring a halt to the circuit breaker is a separate,
higher-stakes cycle):

* ``<history.json>`` — read a promotion history and call
  ``collapse_detector.detect`` (score rising while the frozen anchor degrades).
* ``--floor <task_class>`` — read terminal run outcomes from ``state.db``
  read-only and call ``collapse_detector.floor`` (pass@k falling while lane
  diversity narrows).

``--json`` emits the report as the only stdout.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

from mini_ork.learning import collapse_detector, collapse_floor


def _usage() -> str:
    return (
        "Usage: mini-ork collapse-check <history.json> [--json] [--help]\n"
        "       mini-ork collapse-check --floor <task_class> [--json]\n"
        "\n"
        "Report whether a promotion history is collapsing (score rising while\n"
        "the frozen anchor set degrades). Reads a JSON array of rows, or an\n"
        "object with a \"rows\" key. Always exits 0.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the report as the only stdout\n"
        "  --floor <task_class>  Report the diversity / pass@k floor from state.db (read-only)\n"
        "  --help    This message\n"
    )


def _floor_main(task_class: str, json_flag: bool) -> int:
    path = os.environ.get("MINI_ORK_DB")
    if not path:
        path = os.path.join(
            os.environ.get("MINI_ORK_HOME") or ".mini-ork", "state.db"
        )
    if not os.path.exists(path):
        sys.stderr.write(f"collapse-check: no state.db at {path}\n")
        return 0

    # connect_readonly, not a raw mode=ro URI: on an idle WAL state.db a mode=ro
    # handle fails every query, and the floor reader would then report
    # "insufficient history" instead of the real answer.
    from mini_ork.sqlite_read import connect_readonly  # noqa: PLC0415
    try:
        con = connect_readonly(path)
    except (sqlite3.Error, OSError) as exc:
        sys.stderr.write(f"collapse-check: cannot open {path}: {exc}\n")
        return 0
    try:
        report = collapse_floor.evaluate(con, task_class)
    finally:
        con.close()

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    path = None
    json_flag = False
    floor_class = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_usage())
            return 0
        if a == "--json":
            json_flag = True
        elif a == "--floor":
            if i + 1 >= len(argv):
                sys.stderr.write("--floor requires a task class\n")
                sys.stdout.write(_usage())
                return 0
            floor_class = argv[i + 1]
            i += 1
        elif a.startswith("-"):
            sys.stderr.write(f"unknown flag: {a}\n")
            sys.stdout.write(_usage())
            return 0
        elif path is None:
            path = a
        else:
            sys.stderr.write(f"unexpected argument: {a}\n")
            sys.stdout.write(_usage())
            return 0
        i += 1

    if floor_class is not None:
        if path is not None:
            sys.stderr.write(f"unexpected argument: {path}\n")
            sys.stdout.write(_usage())
            return 0
        return _floor_main(floor_class, json_flag)

    if path is None:
        sys.stderr.write("history.json path required\n")
        sys.stdout.write(_usage())
        return 0

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"collapse-check: cannot read {path}: {exc}\n")
        return 0

    if isinstance(data, dict) and "rows" in data:
        history = data["rows"]
    else:
        history = data

    if not isinstance(history, list):
        sys.stderr.write(
            'collapse-check: input must be a JSON array of rows (or an object with a "rows" key)\n'
        )
        return 0

    try:
        report = collapse_detector.detect(history)
    except (KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"collapse-check: malformed row: {exc}\n")
        return 0

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(collapse_detector.summarize(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
