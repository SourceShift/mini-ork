"""``mini-ork hack-probe`` — report whether a generation history is reward-hacking.

A report-only CLI over a JSON file of generation rows. It reads the rows, calls
``hack_probe.monitor``, and prints the per-test report, the family p-value, and
the verdict. It **always exits 0** — it reports; it does not reselect, promote,
or halt anything (the reselection layer is a separate, higher-stakes cycle).

``--json`` emits the report as the only stdout; every diagnostic — including a
missing or unparseable file — goes to stderr.
"""
from __future__ import annotations

import json
import sys

from mini_ork.learning import hack_probe


def _usage() -> str:
    return (
        "Usage: mini-ork hack-probe <history.json> [--json] [--help]\n"
        "\n"
        "Report whether a generation history is reward-hacking (the visible score\n"
        "rising while the frozen core stays flat or falls). Reads a JSON array of\n"
        "generation rows, or an object with a \"generations\" key. Always exits 0.\n"
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
        lines.append(f"{test['name']}: n={test['n']} p={_fmt(test.get('p'))}")
    family = report["family"]
    lines.append(f"family: k={family['k']} p_family={_fmt(family['p_family'])}")
    verdict = "hacking" if report["hacking"] else "no hacking detected"
    lines.append(f"verdict: {verdict}")
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
        sys.stderr.write(f"hack-probe: cannot read {path}: {exc}\n")
        return 0

    if isinstance(data, dict) and "generations" in data:
        history = data["generations"]
    else:
        history = data

    if not isinstance(history, list):
        sys.stderr.write(
            'hack-probe: input must be a JSON array of generation rows '
            '(or an object with a "generations" key)\n'
        )
        return 0

    try:
        report = hack_probe.monitor(history)
    except (KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(f"hack-probe: malformed row: {exc}\n")
        return 0

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    elif report["n_generations"] < hack_probe.MIN_GENERATIONS:
        sys.stdout.write(f"insufficient history (n < {hack_probe.MIN_GENERATIONS})\n")
    else:
        sys.stdout.write(_render(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
