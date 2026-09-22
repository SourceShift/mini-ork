"""``mini-ork harness-edit`` — propose typed harness edits from failure receipts.

A report-only CLI over a JSON file of failure receipts (and, optionally, the
paired outcome rows used to score each proposal). It reads the file, calls
``harness_operator.summarize``, and prints the proposals and scores. It
**always exits 0** — it reports; it applies nothing (wiring a proposal into a
harness change is a separate, higher-stakes cycle).

``--json`` emits the report as the only stdout; every diagnostic — including a
missing or unparseable file — goes to stderr.
"""
from __future__ import annotations

import json
import sys

from mini_ork.learning import harness_operator


def _usage() -> str:
    return (
        "Usage: mini-ork harness-edit <receipts.json> [--json] [--help]\n"
        "\n"
        "Propose typed harness edits from a JSON array of failure receipts, or an\n"
        "object with a \"receipts\" key and an optional \"rows\" key (paired outcome\n"
        "rows used to score each proposal). Always exits 0.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the report as the only stdout\n"
        "  --help    This message\n"
    )


def _fmt_delta(value) -> str:
    return "-" if value is None else format(value, "+.3f")


def _render(report) -> str:
    proposals = report["proposals"]
    if not proposals:
        return "no failures met the support threshold (min_support=2)"

    lines = []
    for p in proposals:
        lines.append(
            f"{p['signature']}  ->  {p['target']} ({p['kind']})  support={p['support']}"
        )
        lines.append(f"  {p['rationale']}")

    scores = report["scores"]
    if scores:
        lines.append("")
        for p, s in zip(proposals, scores):
            delta = _fmt_delta(s["delta"])
            resolved = "resolved" if s["resolved"] else "unresolved"
            line = f"{p['signature']}: delta={delta} ({resolved})"
            if s["agrees"] is not None:
                verdict = "agrees" if s["agrees"] else "disagrees"
                line += f" — proposer {verdict} with the outcome"
            lines.append(line)
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
        sys.stderr.write("receipts.json path required\n")
        sys.stdout.write(_usage())
        return 0

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"harness-edit: cannot read {path}: {exc}\n")
        return 0

    if isinstance(data, dict) and "receipts" in data:
        receipts = data["receipts"]
        rows = data.get("rows")
    else:
        receipts = data
        rows = None

    if not isinstance(receipts, list):
        sys.stderr.write(
            "harness-edit: input must be a JSON array of receipts "
            '(or an object with a "receipts" key)\n'
        )
        return 0

    report = harness_operator.summarize(receipts, rows=rows)

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
