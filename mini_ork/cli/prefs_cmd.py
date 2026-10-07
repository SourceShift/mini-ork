"""``mini-ork prefs`` — read/write operator preferences and constraints.

Subcommands (kickoff §"`mini-ork prefs`" + §"`prefs preview`"):

    prefs list [--json]
    prefs set <key> <value...> [--scope global|task_class|workflow|path] [--target X]
    prefs rm <key> [--scope …] [--target …]
    prefs preview <kickoff.md> [--task-class X] [--node implementer|researcher|reviewer] [--json]

``prefs preview`` prints EXACTLY the learned block a node of the given type
would receive for that kickoff — the operator-preference block followed by the
learned-failure-modes block, in the same order and with the same headers
``mini_ork.cli.execute._learned_block`` assembles. Steering is deliberately
omitted (it is run-specific, consumed by ``operator_steering.fetch_for``).

Exit codes: 0 on success, 2 on a usage error (message on stderr). This
DIFFERS from ``memory_lifecycle``'s "always 0" — a bad scope is a real
mistake the operator should see as a non-gate.
"""
from __future__ import annotations

import json
import os
import re
import sys

from mini_ork.context import run_context_scope
from mini_ork.memory import preferences
from mini_ork.memory.preferences import (
    list_prefs,
    remove_pref,
    set_pref,
)

_VALID_NODES = ("researcher", "implementer", "reviewer")


def _usage() -> str:
    return (
        "Usage: mini-ork prefs <action> [args]\n"
        "\n"
        "Read/write operator preferences and constraints\n"
        "(scoped: global, task_class, workflow, path).\n"
        "\n"
        "Actions:\n"
        "  prefs list                 List all prefs (DB + legacy files)\n"
        "  prefs set <key> <value...> Upsert a preference\n"
        "  prefs rm <key>             Remove a preference\n"
        "  prefs preview <kickoff.md> Preview the learned block a node would get\n"
        "\n"
        "Options:\n"
        "  --scope <scope>            global | task_class | workflow | path (default: global)\n"
        "  --target <name>            task_class / workflow name, or a file glob for\n"
        "                             scope=path (e.g. 'mini_ork/ide_pages/**')\n"
        "  --task-class <name>        preview: override the kickoff's task class\n"
        "  --node <type>              preview: researcher | implementer | reviewer\n"
        "  --json                     Emit JSON instead of text (list, preview)\n"
        "  --help                     This message\n"
    )


def _parse(argv: list[str]) -> dict:
    """Return a dict of parsed options; ``error`` key on usage error."""
    opts = {
        "action": None,
        "positional": [],
        "scope": "global",
        "target": "",
        "task_class": "",
        "node": "implementer",
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
        if a == "--task-class":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --task-class"
                return opts
            opts["task_class"] = argv[i + 1]
            i += 2
            continue
        if a == "--node":
            if i + 1 >= len(argv):
                opts["error"] = "missing value for --node"
                return opts
            opts["node"] = argv[i + 1]
            i += 2
            continue
        if a in ("list", "set", "rm", "preview"):
            opts["action"] = a
            i += 1
            continue
        opts["positional"].append(a)
        i += 1
    return opts


def _emit_table(rows: list, out) -> None:
    # The target column is wide enough for a path-scope glob (e.g.
    # ``mini_ork/ide_pages/**``) so ``prefs list`` shows the glob, not a
    # truncated prefix.
    out.write(f"{'key':<28} {'value':<40} {'scope':<10} {'target':<40} source\n")
    for r in rows:
        v = str(r.get("value", ""))
        if len(v) > 38:
            v = v[:35] + "..."
        out.write(
            f"{str(r.get('key',''))[:28]:<28} {v[:40]:<40} "
            f"{str(r.get('scope',''))[:10]:<10} "
            f"{str(r.get('target',''))[:40]:<40} "
            f"{r.get('source','')}\n"
        )


def _validate_scope_value(scope: str) -> bool:
    return scope in ("global", "task_class", "workflow", "path")


# ─── preview helpers ──────────────────────────────────────────────────────


def _kickoff_task_class(text: str) -> str:
    """``task_class: <x>`` from the kickoff front matter, else ''."""
    for raw in text.splitlines():
        m = re.match(r"^\s*task_class\s*:\s*(\S+)", raw)
        if m:
            return m.group(1).strip()
    return ""


def _kickoff_scope_paths(text: str) -> list[str]:
    """Backticked path-like tokens in the kickoff's ``## Files in scope`` section.

    Reads the section directly (not ``scope_allow``) so a preview shows the
    files the kickoff actually declares — the run-profile heuristic can leak
    prose into ``scope_allow`` (see ``preferences.scope_paths``).
    """
    buf: list[str] = []
    captured = False
    for raw in text.splitlines():
        m = re.match(r"^\s*#{2,6}\s+(.+?)\s*$", raw)
        if m:
            title = m.group(1).strip().lower()
            if "files in scope" in title:
                captured = True
                continue
            if captured:  # next heading ends the section
                break
            continue
        if captured:
            buf.append(raw)
    return preferences.paths_in_text("\n".join(buf))


def _preview(kickoff_path: str, task_class: str, node: str, as_json: bool,
             out, err) -> int:
    """Print the exact learned block a ``node`` of ``task_class`` would receive.

    Read-only with respect to the retrieval ledger: ``MINI_ORK_RUN_ID`` is masked
    for the block (``run_context_scope({"MINI_ORK_RUN_ID": None})``) because
    ``semantic_lessons_md`` records a retrieval only when it is set
    (context_assembler.py:629) — a preview is not a run and must not log spend.
    """
    try:
        with open(kickoff_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        err.write(f"cannot read kickoff {kickoff_path}: {exc}\n")
        return 2

    tc = task_class or _kickoff_task_class(text) or "framework_edit"
    paths = _kickoff_scope_paths(text)

    from mini_ork import context_assembler

    sources: list[dict] = []
    with run_context_scope({"MINI_ORK_RUN_ID": None}):
        prefs = preferences.prefs_for(tc, paths=paths)
        block = preferences.render_block(prefs)
        if block:
            for p in prefs:
                sources.append({
                    "kind": "preference",
                    "id": f"pref:{p['scope']}:{p['target']}:{p['key']}",
                    "text": p["value"],
                })
        fm = context_assembler.failure_modes_md(
            tc, 5, db=os.environ.get("MINI_ORK_DB"),
            node_type=node, sources=sources,
        ).strip()
        if fm:
            block = block + "\n\n" + fm + "\n"

    if as_json:
        out.write(json.dumps({
            "task_class": tc, "node": node, "paths": paths,
            "block": block, "sources": sources,
        }, ensure_ascii=False) + "\n")
    else:
        out.write(block)
    return 0


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

    if action == "preview":
        pos = opts["positional"]
        if len(pos) != 1:
            err.write(
                "usage: prefs preview <kickoff.md> [--task-class X] "
                "[--node researcher|implementer|reviewer] [--json]\n"
            )
            out.write(_usage())
            return 2
        if opts["node"] not in _VALID_NODES:
            err.write(
                f"invalid --node {opts['node']!r}; "
                f"allowed: {', '.join(_VALID_NODES)}\n"
            )
            out.write(_usage())
            return 2
        return _preview(pos[0], opts["task_class"], opts["node"],
                        opts["json"], out, err)

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