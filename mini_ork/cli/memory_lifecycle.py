"""``mini-ork memory-lifecycle`` — report the retirement state of the semantic
memory ledger.

A reporting CLI over the reversible retirement lifecycle. It **always exits 0**:
it surfaces which memories are obsolescence (or recovery) candidates and lets a
caller retire / reactivate an id explicitly, but it never retires anything on
its own and never gates a run on the answer.

Human output is a table (for ``--candidates``) or a single payload line (for
``--state`` / ``--retire`` / ``--reactivate``). ``--json`` emits the underlying
payload as the only stdout so callers can pipe it.
"""
from __future__ import annotations

import json
import sys

from mini_ork.memory import (
    RETIRE_ENTER_UTILITY,
    RETIRE_EXIT_UTILITY,
    RETIRE_MIN_USES,
    candidates,
    reactivate,
    retire,
    retirement_state,
)


def _usage() -> str:
    return (
        "Usage: mini-ork memory-lifecycle <action> [options]\n"
        "\n"
        "Report the reversible retirement state of the semantic memory ledger.\n"
        "This command reports; it never retires anything on its own and always\n"
        "exits 0 (it does not gate a run).\n"
        "\n"
        "Actions:\n"
        "  --candidates         List obsolescence candidates in --scope\n"
        "  --retire ID          Retire memory ID (requires --reason)\n"
        "  --reactivate ID      Reactivate memory ID\n"
        "  --state ID           Print the retirement state of memory ID\n"
        "\n"
        "Options:\n"
        "  --scope <scope>      Scope to scan (required with --candidates)\n"
        "  --reason <text>      Why this memory is being retired (with --retire)\n"
        "  --enter <float>      Retire threshold (default: 0.25)\n"
        "  --exit <float>       Reactivate threshold (default: 0.45)\n"
        "  --min-uses <int>     Minimum uses for a retire candidate (default: 4)\n"
        "  --json               Emit JSON instead of a table\n"
        "  --help               This message\n"
    )


def _parse(argv):
    """Return a dict of parsed options (never raises; errors are reported via
    the ``error`` key)."""
    opts = {
        "scope": None,
        "action": None,
        "target": None,
        "reason": None,
        "enter": RETIRE_ENTER_UTILITY,
        "exit": RETIRE_EXIT_UTILITY,
        "min_uses": RETIRE_MIN_USES,
        "json": False,
        "help": False,
        "error": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            opts["help"] = True
            i += 1
        elif a == "--candidates":
            opts["action"] = "candidates"
            i += 1
        elif a == "--retire":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --retire"
                break
            try:
                opts["target"] = int(argv[i + 1])
            except ValueError:
                opts["error"] = f"invalid memory id: {argv[i + 1]}"
                break
            opts["action"] = "retire"
            i += 2
        elif a == "--reactivate":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --reactivate"
                break
            try:
                opts["target"] = int(argv[i + 1])
            except ValueError:
                opts["error"] = f"invalid memory id: {argv[i + 1]}"
                break
            opts["action"] = "reactivate"
            i += 2
        elif a == "--state":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --state"
                break
            try:
                opts["target"] = int(argv[i + 1])
            except ValueError:
                opts["error"] = f"invalid memory id: {argv[i + 1]}"
                break
            opts["action"] = "state"
            i += 2
        elif a == "--scope":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --scope"
                break
            opts["scope"] = argv[i + 1]
            i += 2
        elif a == "--reason":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --reason"
                break
            opts["reason"] = argv[i + 1]
            i += 2
        elif a == "--enter":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --enter"
                break
            try:
                opts["enter"] = float(argv[i + 1])
            except ValueError:
                opts["error"] = f"invalid --enter value: {argv[i + 1]}"
                break
            i += 2
        elif a == "--exit":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --exit"
                break
            try:
                opts["exit"] = float(argv[i + 1])
            except ValueError:
                opts["error"] = f"invalid --exit value: {argv[i + 1]}"
                break
            i += 2
        elif a == "--min-uses":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --min-uses"
                break
            try:
                opts["min_uses"] = int(argv[i + 1])
            except ValueError:
                opts["error"] = f"invalid --min-uses value: {argv[i + 1]}"
                break
            i += 2
        elif a == "--json":
            opts["json"] = True
            i += 1
        else:
            opts["error"] = f"unknown flag: {a}"
            break
    return opts


def main(argv=None, *, stdout=None, stderr=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    opts = _parse(sys.argv[1:] if argv is None else argv)

    if opts["help"]:
        out.write(_usage())
        return 0
    if opts["error"]:
        err.write(f"{opts['error']}\n")
        out.write(_usage())
        return 0  # reporting CLI never gates a run

    action = opts["action"]

    if action == "candidates":
        if not opts["scope"]:
            err.write("--candidates requires --scope\n")
            out.write(_usage())
            return 0
        try:
            rows = candidates(
                opts["scope"],
                enter=opts["enter"],
                exit=opts["exit"],
                min_uses=opts["min_uses"],
            )
        except ValueError as exc:
            err.write(f"{exc}\n")
            return 0
        if opts["json"]:
            out.write(json.dumps(rows) + "\n")
        else:
            out.write(
                f"{'id':>4} {'state':>8} {'recommendation':>15} "
                f"{'uses':>6} {'wins':>6} {'utility':>9}\n"
            )
            for r in rows:
                out.write(
                    f"{r['memory_id']:>4} {r['state']:>8} "
                    f"{r['recommendation']:>15} {r['uses']:>6} "
                    f"{r['wins']:>6} {r['utility']:>9.4f}\n"
                )
        return 0

    if action == "retire":
        if not opts["reason"] or not opts["reason"].strip():
            err.write("--retire requires a non-empty --reason\n")
            return 0
        try:
            ok = retire(opts["target"], opts["reason"])
        except ValueError as exc:
            err.write(f"{exc}\n")
            return 0
        if opts["json"]:
            out.write(json.dumps({"memory_id": opts["target"], "retired": ok}) + "\n")
        else:
            out.write(f"{'retired' if ok else 'unknown id'}: {opts['target']}\n")
        return 0

    if action == "reactivate":
        ok = reactivate(opts["target"])
        if opts["json"]:
            out.write(json.dumps({"memory_id": opts["target"], "reactivated": ok}) + "\n")
        else:
            out.write(f"{'reactivated' if ok else 'unknown id'}: {opts['target']}\n")
        return 0

    if action == "state":
        st = retirement_state(opts["target"])
        if opts["json"]:
            out.write(json.dumps(st) + "\n")
        elif st is None:
            out.write(f"unknown id: {opts['target']}\n")
        else:
            out.write(
                f"id={st['memory_id']} retired={st['retired']} "
                f"retired_at={st['retired_at']} reason={st['reason']!r} "
                f"evidence={st['evidence']!r}\n"
            )
        return 0

    out.write(_usage())
    return 0


if __name__ == "__main__":
    sys.exit(main())
