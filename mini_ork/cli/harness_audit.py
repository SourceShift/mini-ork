"""``mini-ork harness-audit`` — audit applied harness edits for tampering.

A report-only CLI over a JSON file of harness-edit observations. It reads the
file, calls ``harness_integrity.audit`` / ``harness_integrity.summarize``, and
prints the per-edit findings and the system profile. It **always exits 0** — it
reports; it does not halt, block, revert, or promote anything.

``--json`` emits the report as the only stdout; every diagnostic — including a
missing or unparseable file and an unknown role — goes to stderr.
"""
from __future__ import annotations

import json
import sys

from mini_ork.learning import harness_integrity


def _usage() -> str:
    return (
        "Usage: mini-ork harness-audit <edits.json> [--json] [--help]\n"
        "\n"
        "Audit applied harness edits from a JSON array of edit observations, or\n"
        "an object with an \"edits\" key. Each row may carry a \"score_delta\"; a\n"
        "row without one is audited as undecided. Always exits 0.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the report as the only stdout\n"
        "  --help    This message\n"
    )


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return format(value, ".3f")
    return str(value)


def _render(reports, summary) -> str:
    lines = []
    for report in reports:
        surface = report["surface"]
        role = report["role"]
        if report["violations"]:
            tags = " ".join(f"{role}/{o}" for o in report["violations"])
            lines.append(f"{surface} [{role}]: {tags}")
        else:
            lines.append(f"{surface} [{role}]: clean")
    lines.append(
        f"summary: n={summary['n']} n_tampering={summary['n_tampering']} "
        f"rate_tampering={_fmt(summary['rate_tampering'])} "
        f"rate_illusory={_fmt(summary['rate_illusory'])}"
    )
    for report in reports:
        for obligation in report["undecided"]:
            lines.append(
                f"undecided: {report['surface']} [{report['role']}]: {obligation}"
            )
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
        sys.stderr.write("edits.json path required\n")
        sys.stdout.write(_usage())
        return 0

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"harness-audit: cannot read {path}: {exc}\n")
        return 0

    if isinstance(data, dict) and "edits" in data:
        edits = data["edits"]
    else:
        edits = data

    if not isinstance(edits, list):
        sys.stderr.write(
            "harness-audit: input must be a JSON array of edit observations "
            '(or an object with an "edits" key)\n'
        )
        return 0

    if not edits:
        if json_flag:
            payload = {"summary": harness_integrity.summarize([]), "edits": []}
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write("no edits to audit\n")
        return 0

    try:
        reports = [harness_integrity.audit(e) for e in edits]
        summary = harness_integrity.summarize(edits)
    except ValueError as exc:
        sys.stderr.write(f"harness-audit: {exc}\n")
        return 0

    if json_flag:
        payload = {
            "summary": summary,
            "edits": [
                {
                    "surface": r["surface"],
                    "role": r["role"],
                    "family": r["family"],
                    "labels": [f"{r['role']}/{o}" for o in r["violations"]],
                    "violations": r["violations"],
                    "undecided": r["undecided"],
                    "decidable": r["decidable"],
                    "tampering": r["tampering"],
                    "verdict": harness_integrity.verdict(
                        e, score_delta=e.get("score_delta")
                    )["verdict"],
                }
                for e, r in zip(edits, reports)
            ],
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render(reports, summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
