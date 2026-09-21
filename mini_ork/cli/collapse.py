"""``mini-ork collapse-check`` — report whether a promotion history is collapsing.

A report-only CLI over a history JSON file. It reads the rows, calls
``collapse_detector.detect``, and prints the report. It **always exits 0** — it
reports; it does not halt anything (wiring the halt to the circuit breaker is a
separate, higher-stakes cycle).

``--json`` emits the report as the only stdout.
"""
from __future__ import annotations

import json
import sys

from mini_ork.learning import collapse_detector


def _usage() -> str:
    return (
        "Usage: mini-ork collapse-check <history.json> [--json] [--help]\n"
        "\n"
        "Report whether a promotion history is collapsing (score rising while\n"
        "the frozen anchor set degrades). Reads a JSON array of rows, or an\n"
        "object with a \"rows\" key. Always exits 0.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the report as the only stdout\n"
        "  --help    This message\n"
    )


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    path = None
    json_flag = False
    for a in argv:
        if a in ("--help", "-h"):
            sys.stdout.write(_usage())
            return 0
        if a == "--json":
            json_flag = True
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
