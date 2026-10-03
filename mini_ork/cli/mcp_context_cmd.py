"""``mini-ork mcp-context`` — run the read-only stdio MCP context server.

Thin CLI over :mod:`mini_ork.mcp_context.server`: checks ``--help``, then
hands stdin/stdout to ``server.serve``. ``stdout`` is the MCP protocol
wire, so every diagnostic this module emits goes to stderr and importing
it prints nothing — the dispatcher runs the module as
``python -m mini_ork.cli.mcp_context_cmd`` (see ``_native_module_handler``
in :mod:`mini_ork.cli.main`).
"""
from __future__ import annotations

import os
import sys

_USAGE = (
    "Usage: mini-ork mcp-context\n\n"
    "Run the read-only stdio MCP server exposing mini-ork runs, learnings,\n"
    "cost, and lane map to agents (e.g. inside Zed). The server runs until\n"
    "stdin closes.\n"
)


def main(rest: list[str], root: str) -> int:
    del root
    if rest and rest[0] in ("--help", "-h"):
        sys.stderr.write(_USAGE)
        return 0
    if rest:
        sys.stderr.write(f"mini-ork mcp-context: unexpected argument: {rest[0]}\n")
        sys.stderr.write(_USAGE)
        return 2

    from mini_ork.mcp_context.server import serve

    return serve()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", "")))