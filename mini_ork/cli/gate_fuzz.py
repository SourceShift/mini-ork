"""``mini-ork gate-fuzz`` — run the hermetic gate fuzzer and report its rates.

Runs ``fuzz_gate`` over a probe corpus against the shipped artifact_contract
gate and prints the blind-spot / over-block rates. This is a measurement, not a
gate: a non-zero blind-spot rate is the finding, so the run exits 0 and feeds
the loop rather than red-failing it. ``--json`` emits the full report as the
only stdout.

With ``--hackability --gate ID ...`` it instead attacks the named registered
gate(s) with known-bad ("hollow") inputs and persists
``hackability = passed_bad / trials`` next to the DB. The legacy (no
``--hackability``) path is byte-identical to before.
"""
from __future__ import annotations

import json
import sys
import tempfile

from mini_ork.context import RunContext
from mini_ork.gates import gate_fuzzer


def _usage() -> str:
    return (
        "Usage: mini-ork gate-fuzz [--corpus PATH] [--json] [--help]\n"
        "       mini-ork gate-fuzz --hackability --gate ID [--gate ID ...]\n"
        "                          [--db PATH] [--proposer-lane LANE] [--json]\n"
        "\n"
        "Run a hermetic gate fuzzer over a probe corpus and report the\n"
        "blind-spot and over-block rates. Always exits 0 on a measurement.\n"
        "\n"
        "Options:\n"
        "  --corpus PATH  Probe corpus JSON array (default: the shipped corpus)\n"
        "  --json         Emit the full report as JSON (the only stdout)\n"
        "  --help         This message\n"
        "\n"
        "Hackability audit (--hackability):\n"
        "  --hackability  Attack registered gate(s) with known-bad inputs\n"
        "  --gate ID      Gate to audit (repeatable); required with --hackability\n"
        "  --db PATH      State DB (default: $MINI_ORK_DB / $MINI_ORK_HOME/state.db)\n"
        "  --proposer-lane LANE  Optional LLM lane to propose evidence documents\n"
    )


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if "--hackability" in argv:
        return _main_hackability(argv)
    return _main_legacy(argv)


def _main_legacy(argv) -> int:
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


def _usage_error(message: str) -> int:
    sys.stderr.write(f"gate-fuzz: {message}\n")
    sys.stdout.write(_usage())
    return 2


def _main_hackability(argv) -> int:
    from mini_ork.gates import gate_registry, hackability

    gates: list[str] = []
    db_path: str | None = None
    proposer_lane: str | None = None
    json_flag = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--hackability":
            i += 1
        elif a == "--gate":
            if i + 1 >= len(argv):
                return _usage_error("--gate requires a gate id")
            gates.append(argv[i + 1])
            i += 2
        elif a == "--db":
            if i + 1 >= len(argv):
                return _usage_error("--db requires a path")
            db_path = argv[i + 1]
            i += 2
        elif a == "--proposer-lane":
            if i + 1 >= len(argv):
                return _usage_error("--proposer-lane requires a lane")
            proposer_lane = argv[i + 1]
            i += 2
        elif a == "--json":
            json_flag = True
            i += 1
        elif a == "--corpus":
            return _usage_error("--hackability cannot be combined with --corpus")
        elif a in ("--help", "-h"):
            sys.stdout.write(_usage())
            return 0
        else:
            return _usage_error(f"Unknown flag: {a}")

    if not gates:
        return _usage_error("--hackability requires at least one --gate ID")

    if db_path is None:
        db_path = RunContext.from_env().db_or_default()

    # Validate every gate before auditing any: an unknown or inactive gate
    # exits rc 2 and writes nothing.
    active_ids = {g["gate_id"] for g in gate_registry.gate_list(db_path)}
    for gid in gates:
        if gid not in active_ids:
            return _usage_error(f"unknown or inactive gate: {gid}")

    records: list[dict] = []
    for gid in gates:
        proposer = hackability.lane_proposer(proposer_lane) if proposer_lane else None
        record = hackability.audit_gate(db_path, gid, proposer=proposer)
        path = hackability.write_record(db_path, record)
        records.append(record)
        if json_flag:
            continue
        hk = record["hackability"]
        hk_s = "-" if hk is None else f"{hk:.3f}"
        unverified = record["unverified"]
        sys.stdout.write(
            f"gate-hackability: gate={gid} hackability={hk_s} "
            f"passed_bad={record['passed_bad']}/{record['trials']} "
            f"unverified=crashed:{unverified['crashed']},"
            f"not_known_bad:{unverified['not_known_bad']} "
            f"proposer={record['proposer_status']} record={path}\n"
        )
        operator_by_id = {r["id"]: r["operator"] for r in record["results"]}
        for exploit in record["exploits"]:
            sys.stdout.write(f"  exploit: {exploit} ({operator_by_id[exploit]})\n")

    if json_flag:
        sys.stdout.write(json.dumps({"records": records}, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
