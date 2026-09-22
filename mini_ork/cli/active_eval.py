"""``mini-ork active-eval`` — report which scenario to probe next.

A report-only CLI over a JSON file of scenario rows. It reads the rows, calls
``active_eval.report``, and prints the proxy/target rates, the transfer
residual, and the selection. It **always exits 0** — it reports; it does not
probe, test a target, label a scenario, or edit any corpus.

``--json`` emits the report as the only stdout; every diagnostic — including a
missing or unparseable file — goes to stderr.
"""
from __future__ import annotations

import json
import sys

from mini_ork.learning import active_eval


def _usage() -> str:
    return (
        "Usage: mini-ork active-eval <history.json> [--budget N] [--json] [--help]\n"
        "\n"
        "Report which scenario to probe next (arXiv 2608.13719). Reads a JSON\n"
        "array of scenario rows, or an object with a \"scenarios\" key. Always\n"
        "exits 0.\n"
        "\n"
        "Options:\n"
        "  --budget N  Maximum scenarios to select (default 1)\n"
        "  --json      Emit the report as the only stdout\n"
        "  --help      This message\n"
    )


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def _render(report, resid_p) -> str:
    proxy = report["proxy"]
    target = report["target"]
    lines = [
        f"proxy: n={proxy['n']} rate={_fmt(proxy['rate'])}",
        f"target: n={target['n']} rate={_fmt(target['rate'])}",
        f"transfer-residual: residual={_fmt(report['transfer_residual'])} "
        f"p={_fmt(resid_p)}",
    ]
    rank = report["rank"]
    if rank["selected"]:
        lines.append("selected: " + ", ".join(rank["selected"]))
    else:
        reasons = report["undecided"]
        lines.append("undecided: " + (", ".join(reasons) if reasons else "-"))
        if report["n_paired"] < active_eval.MIN_PAIRED:
            lines.append(
                f"insufficient paired observations (n < {active_eval.MIN_PAIRED})"
            )
    return "\n".join(lines)


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    path = None
    json_flag = False
    budget = 1
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_usage())
            return 0
        if a == "--json":
            json_flag = True
        elif a == "--budget":
            i += 1
            if i >= len(argv):
                sys.stderr.write("--budget requires a value\n")
                sys.stdout.write(_usage())
                return 0
            try:
                budget = int(argv[i])
            except ValueError:
                sys.stderr.write(f"invalid budget: {argv[i]}\n")
                sys.stdout.write(_usage())
                return 0
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

    if path is None:
        sys.stderr.write("history.json path required\n")
        sys.stdout.write(_usage())
        return 0

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"active-eval: cannot read {path}: {exc}\n")
        return 0

    if isinstance(data, dict) and "scenarios" in data:
        history = data["scenarios"]
    else:
        history = data

    if not isinstance(history, list):
        sys.stderr.write(
            'active-eval: input must be a JSON array of scenario rows '
            '(or an object with a "scenarios" key)\n'
        )
        return 0

    try:
        report = active_eval.report(history, budget)
        resid_p = active_eval.residual_p(history)
    except (KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"active-eval: malformed row: {exc}\n")
        return 0

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render(report, resid_p) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
