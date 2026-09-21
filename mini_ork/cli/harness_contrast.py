"""``mini-ork harness-contrast`` — attribute an A-vs-B harness contrast.

A report-only CLI over a JSON file of paired rows. It reads the rows, calls
``attribute``, and prints the attribution report. It **always exits 0**: a
contaminated input (empty or mixed lane) is a finding to surface — "the
contrast cannot be attributed" — not a crash, and never a gate on a run.

``--json`` emits the report as the only stdout; on contamination it emits
``{"error": ..., "resolved": false}`` so a caller can distinguish "unresolved"
from "not measured".
"""
from __future__ import annotations

import json
import sys

from mini_ork.learning import harness_contrast


def _usage() -> str:
    return (
        "Usage: mini-ork harness-contrast <rows.json> [--json] [--help]\n"
        "\n"
        "Attribute an A-vs-B harness contrast from a JSON array of paired rows.\n"
        "Each row is {\"lane\": <str>, \"a\": <bool>, \"b\": <bool>}. Always exits 0.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the report (or {\"error\": ...}) as the only stdout\n"
        "  --help    This message\n"
    )


def _emit_error(message: str, json_flag: bool) -> None:
    if json_flag:
        sys.stdout.write(
            json.dumps({"error": message, "resolved": False}) + "\n"
        )
    else:
        sys.stderr.write(f"harness-contrast: {message}\n")


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
        sys.stderr.write("rows.json path required\n")
        sys.stdout.write(_usage())
        return 0

    try:
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _emit_error(f"cannot read {path}: {exc}", json_flag)
        return 0

    if not isinstance(rows, list):
        _emit_error("input must be a JSON array of paired rows", json_flag)
        return 0

    try:
        report = harness_contrast.attribute(rows)
    except ValueError as exc:
        _emit_error(str(exc), json_flag)
        return 0

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(harness_contrast.summarize(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
