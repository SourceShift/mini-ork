"""``mini-ork metric-anchor`` — report anchor discipline for an eval metric.

A report-only CLI over a JSON file of metric generations. It reads the rows,
calls ``metric_anchor.audit``, and prints the anchor report, the vacuity
report, and the intact verdict. It **always exits 0** — it reports; it does not
retrain, replace, disable, or promote any metric.

``--json`` emits the report as the only stdout; every diagnostic — including a
missing or unparseable file — goes to stderr.
"""
from __future__ import annotations

import json
import sys

from mini_ork.learning import metric_anchor


def _usage() -> str:
    return (
        "Usage: mini-ork metric-anchor <history.json> [--json] [--help]\n"
        "\n"
        "Report anchor discipline for an evolved eval metric (arXiv 2607.12790).\n"
        "Reads a JSON array of metric generations, or an object with a\n"
        "\"generations\" key. Always exits 0.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the report as the only stdout\n"
        "  --help    This message\n"
    )


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def _render(report) -> str:
    anchor = report["anchor"]
    vac = report["vacuity"]
    lines = [
        f"anchor: n={anchor['n']} n_contaminated={anchor['n_contaminated']} "
        f"rate_contaminated={_fmt(anchor['rate_contaminated'])}",
        f"vacuity: latest={_fmt(vac['latest'])} delta={_fmt(vac['delta'])} "
        f"p={_fmt(vac['p'])}",
    ]
    intact_value = report["intact"]
    if intact_value is None:
        reasons = report["undecided"]
        lines.append("undecided: " + (", ".join(reasons) if reasons else "-"))
        if report["n_with_anchor"] < metric_anchor.MIN_GENERATIONS:
            lines.append(
                f"insufficient history (n < {metric_anchor.MIN_GENERATIONS})"
            )
    else:
        lines.append(f"intact: {intact_value}")
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
        sys.stderr.write(f"metric-anchor: cannot read {path}: {exc}\n")
        return 0

    if isinstance(data, dict) and "generations" in data:
        history = data["generations"]
    else:
        history = data

    if not isinstance(history, list):
        sys.stderr.write(
            'metric-anchor: input must be a JSON array of generation rows '
            '(or an object with a "generations" key)\n'
        )
        return 0

    try:
        report = metric_anchor.audit(history)
    except (KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"metric-anchor: malformed row: {exc}\n")
        return 0

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
