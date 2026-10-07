"""``mini-ork prefs`` — read/write operator preferences and constraints.

Subcommands (kickoff §"`mini-ork prefs`"):

    prefs list [--json]
    prefs set <key> <value...> [--scope global|task_class|workflow] [--target X]
    prefs rm <key> [--scope …] [--target …]

Exit codes: 0 on success, 2 on a usage error (message on stderr). This
DIFFERS from ``memory_lifecycle``'s "always 0" — a bad scope is a real
mistake the operator should see as a non-gate.
"""
from __future__ import annotations

import json
import sys

from mini_ork.memory.preferences import (
    list_prefs,
    remove_pref,
    set_pref,
)


def _usage() -> str:
    return (
        "Usage: mini-ork prefs <action> [args]\n"
        "\n"
        "Read/write operator preferences and constraints\n"
        "(scoped: global, task_class, workflow).\n"
        "\n"
        "Actions:\n"
        "  prefs list                 List all prefs (DB + legacy files)\n"
        "  prefs set <key> <value...> Upsert a preference\n"
        "  prefs rm <key>             Remove a preference\n"
        "\n"
        "Options:\n"
        "  --scope <scope>            global | task_class | workflow (default: global)\n"
        "  --target <name>            task_class or workflow name (required for scoped)\n"
        "  --json                     Emit JSON instead of a table (list)\n"
        "  --help                     This message\n"
    )


def _parse(argv: list[str]) -> dict:
    """Return a dict of parsed options; ``error`` key on usage error."""
    opts = {
        "action": None,
        "positional": [],
        "scope": "global",
        "target": "",
        "json": False,
        "help": False,
        "error": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            opts["help"] = True
            return opts
        if a == "--json":
            opts["json"] = True
            i += 1
            continue
        if a == "--scope":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --scope"
                return opts
            opts["scope"] = argv[i + 1]
            i += 2
            continue
        if a == "--target":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --target"
                return opts
            opts["target"] = argv[i + 1]
            i += 2
            continue
        if a in ("list", "set", "rm"):
            opts["action"] = a
            i += 1
            continue
        opts["positional"].append(a)
        i += 1
    return opts


def _emit_table(rows: list, out) -> None:
    out.write(f"{'key':<28} {'value':<40} {'scope':<10} {'target':<14} source\n")
    for r in rows:
        v = str(r.get("value", ""))
        if len(v) > 38:
            v = v[:35] + "..."
        out.write(
            f"{str(r.get('key',''))[:28]:<28} {v[:40]:<40} "
            f"{str(r.get('scope',''))[:10]:<10} "
            f"{str(r.get('target',''))[:14]:<14} "
            f"{r.get('source','')}\n"
        )


def _validate_scope_value(scope: str) -> bool:
    return scope in ("global", "task_class", "workflow")


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
        return 2

    action = opts["action"]
    if action is None:
        if opts["positional"]:
            err.write(f"unknown sub-action: {opts['positional'][0]}\n")
        else:
            err.write("missing sub-action\n")
        out.write(_usage())
        return 2

    if not _validate_scope_value(opts["scope"]):
        err.write(
            f"invalid scope {opts['scope']!r}; "
            "allowed: global, task_class, workflow\n"
        )
        out.write(_usage())
        return 2

    if action == "list":
        rows = list_prefs()
        if opts["json"]:
            out.write(json.dumps(rows, ensure_ascii=False) + "\n")
        else:
            _emit_table(rows, out)
        return 0

    if action == "set":
        pos = opts["positional"]
        if len(pos) < 2:
            err.write("usage: prefs set <key> <value...>\n")
            out.write(_usage())
            return 2
        key = pos[0]
        value = " ".join(pos[1:])
        if opts["scope"] == "global" and opts["target"]:
            err.write("scope=global requires --target '' (omit --target)\n")
            out.write(_usage())
            return 2
        try:
            set_pref(key, value, scope=opts["scope"], target=opts["target"])
        except ValueError as exc:
            err.write(f"{exc}\n")
            return 2
        return 0

    if action == "rm":
        pos = opts["positional"]
        if len(pos) != 1:
            err.write("usage: prefs rm <key> [--scope …] [--target …]\n")
            out.write(_usage())
            return 2
        key = pos[0]
        if opts["scope"] == "global" and opts["target"]:
            err.write("scope=global requires --target '' (omit --target)\n")
            out.write(_usage())
            return 2
        try:
            ok = remove_pref(key, scope=opts["scope"], target=opts["target"])
        except ValueError as exc:
            err.write(f"{exc}\n")
            return 2
        if not ok:
            err.write(f"no such pref: {key} (scope={opts['scope']}, target={opts['target']!r})\n")
        return 0

    err.write(f"unknown sub-action: {action}\n")
    out.write(_usage())
    return 2


if __name__ == "__main__":
    sys.exit(main())