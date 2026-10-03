"""``mini-ork mcp-context`` — run the stdio MCP context server.

Thin CLI over :mod:`mini_ork.mcp_context.server`: checks ``--help``,
parses an optional ``--control`` flag (also accepted as
``MO_MCP_CONTROL=1`` in the environment), then hands stdin/stdout to
``server.serve``. ``stdout`` is the MCP protocol wire, so every
diagnostic this module emits goes to stderr and importing it prints
nothing — the dispatcher runs the module as
``python -m mini_ork.cli.mcp_context_cmd`` (see ``_native_module_handler``
in :mod:`mini_ork.cli.main`).
"""
from __future__ import annotations

import os
import sys

_USAGE = (
    "Usage: mini-ork mcp-context [--control]\n\n"
    "Run the stdio MCP server exposing mini-ork runs, learnings, cost, and\n"
    "lane map to agents (e.g. inside Zed). By default only read-only tools\n"
    "are exposed; with --control (or MO_MCP_CONTROL=1) the server also\n"
    "exposes list_recipes, start_run, run_status, wait_for_run, stop_run,\n"
    "and certify. The server runs until stdin closes.\n"
)


def main(rest: list[str], root: str) -> int:
    del root
    if rest and rest[0] in ("--help", "-h"):
        sys.stderr.write(_USAGE)
        return 0

    # Parse --control BEFORE the unknown-arg rejection so `mini-ork
    # mcp-context --control` doesn't surface rc=2. Env fallback so an
    # operator can flip the bit without re-launching Zed's MCP entry.
    control = False
    consumed: list[str] = []
    for tok in rest:
        if tok == "--control":
            control = True
            consumed.append(tok)
            continue
        consumed.append(tok)
    if os.environ.get("MO_MCP_CONTROL") == "1":
        control = True

    remaining = [t for t in consumed if t != "--control"]
    if remaining:
        sys.stderr.write(f"mini-ork mcp-context: unexpected argument: {remaining[0]}\n")
        sys.stderr.write(_USAGE)
        return 2

    from mini_ork.mcp_context.server import serve

    return serve(control=control)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", "")))