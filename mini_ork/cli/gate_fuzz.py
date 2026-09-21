"""``mini-ork gate-fuzz`` — run the hermetic gate fuzzer and report its rates.

Runs ``fuzz_gate`` over a probe corpus against the shipped artifact_contract
gate and prints the blind-spot / over-block rates. This is a measurement, not a
gate: a non-zero blind-spot rate is the finding, so the run exits 0 and feeds
the loop rather than red-failing it. ``--json`` emits the full report as the
only stdout.
"""
from __future__ import annotations

import json
import sys
import tempfile

from mini_ork.gates import gate_fuzzer


def _usage() -> str:
    return (
        "Usage: mini-ork gate-fuzz [--corpus PATH] [--json] [--help]\n"
        "\n"
        "Run a hermetic gate fuzzer over a probe corpus and report the\n"
        "blind-spot and over-block rates. Always exits 0 on a measurement.\n"
        "\n"
        "Options:\n"
        "  --corpus PATH  Probe corpus JSON array (default: the shipped corpus)\n"
        "  --json         Emit the full report as JSON (the only stdout)\n"
        "  --help         This message\n"
    )


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    corpus = gate_fuzzer.DEFAULT_CORPUS
    json_flag = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            sys.stdout.write(_usage())
            return 0
        if a == "--corpus":
            if i + 1 >= len(argv):
                sys.stderr.write("--corpus requires a path\n")
                sys.stdout.write(_usage())
                return 2
            corpus = argv[i + 1]
            i += 2
        elif a == "--json":
            json_flag = True
            i += 1
        else:
            sys.stderr.write(f"Unknown flag: {a}\n")
            sys.stdout.write(_usage())
            return 2

    try:
        cases = gate_fuzzer.load_corpus(corpus)
    except ValueError as exc:
        sys.stderr.write(f"gate-fuzz: {exc}\n")
        return 2

    with tempfile.TemporaryDirectory() as workdir:
        report = gate_fuzzer.fuzz_gate(
            gate_fuzzer.artifact_contract_evaluator(workdir), cases
        )

    if json_flag:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0

    sys.stdout.write(gate_fuzzer.summarize(report) + "\n")
    for r in report["results"]:
        if r["ok"] or r["got"] == "defer":
            continue
        kind = "blind spot" if r["expect"] == "fail" else "over block"
        sys.stdout.write(f"  {kind}: {r['id']} (got {r['got']})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
