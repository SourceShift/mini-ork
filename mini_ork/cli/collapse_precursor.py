"""``mini-ork collapse-precursor`` — report silent-collapse precursors.

A report-only CLI over a JSON file of generation rows. It reads the rows, calls
``collapse_precursor.monitor``, and prints the per-precursor report, the family
p-value, the lead-time line, and the unmapped precursor. It **always exits 0** —
it reports; it does not halt, block, revert, regulate, or promote anything.

``--json`` emits the report as the only stdout; every diagnostic — including a
missing or unparseable file — goes to stderr.
"""
from __future__ import annotations

import json
import sys

from mini_ork.learning import collapse_precursor


def _usage() -> str:
    return (
        "Usage: mini-ork collapse-precursor <history.json> [--json] [--help]\n"
        "\n"
        "Report silent-collapse precursors over a generation history (the anchor\n"
        "entropy contracting and tail coverage eroding before the visible score\n"
        "degrades). Reads a JSON array of generation rows, or an object with a\n"
        "\"generations\" key. Always exits 0.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the report as the only stdout\n"
        "  --help    This message\n"
    )


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def _render(report) -> str:
    lines = []
    for test in report["tests"]:
        lines.append(
            f"{test['name']}: n={test['n']} delta={_fmt(test.get('delta'))} "
            f"p={_fmt(test.get('p'))}"
        )
    family = report["family"]
    lines.append(f"family: k={family['k']} p_family={_fmt(family['p_family'])}")
    lead = report["lead"]
    lines.append(
        f"lead: precursor_gen={_fmt(lead['precursor_gen'])} "
        f"standard_gen={_fmt(lead['standard_gen'])} lead={_fmt(lead['lead'])} "
        f"early_warning={lead['early_warning']}"
    )
    unmapped = report["precursors_unmapped"]
    lines.append("unmapped: " + (", ".join(unmapped) if unmapped else "-"))
    return "\n".join(lines)


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
        sys.stderr.write(f"collapse-precursor: cannot read {path}: {exc}\n")
        return 0

    if isinstance(data, dict) and "generations" in data:
        history = data["generations"]
    else:
        history = data

    if not isinstance(history, list):
        sys.stderr.write(
            'collapse-precursor: input must be a JSON array of generation rows '
            '(or an object with a "generations" key)\n'
        )
        return 0

    try:
        report = collapse_precursor.monitor(history)
    except (KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"collapse-precursor: malformed row: {exc}\n")
        return 0

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    elif report["n_generations"] < collapse_precursor.MIN_GENERATIONS:
        sys.stdout.write(
            f"insufficient history (n < {collapse_precursor.MIN_GENERATIONS})\n"
        )
    else:
        sys.stdout.write(_render(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
