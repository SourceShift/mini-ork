"""``mini-ork acp`` — run the stdio ACP agent (session id == run id)
or run the first-run interactive setup (``mini-ork acp --setup``).

Thin CLI over :mod:`mini_ork.acp.agent`: checks the optional ``acp`` extra is
importable, then hands stdin/stdout to ``acp.run_agent``. ``--setup`` runs
without the ``acp`` extra: it is the first-run check (project, orchestrator
login, worker keys) the editor opens in a terminal as ACP terminal auth.
stdout is the ACP wire, so every diagnostic this module emits goes to stderr
and importing it prints nothing.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import cast

_USAGE = (
    "Usage: mini-ork acp [--setup]\n\n"
    "Run the stdio ACP agent (one session == one run id), or run the first-\n"
    "run setup that checks the project, the orchestrator's Claude login,\n"
    "and the worker lanes' keys, and fixes what is missing.\n"
    "\n"
    "  (no flag)  start the stdio ACP agent (default).\n"
    "  --setup    run the interactive first-run setup (no 'acp' extra needed).\n"
    "  --help     show this help and exit.\n"
)


def main(rest: list[str], root: str) -> int:
    del root
    if rest and rest[0] in ("--help", "-h"):
        sys.stderr.write(_USAGE)
        return 0
    # ``--setup`` must run BEFORE the ``import acp`` block; it does not need
    # the optional ``acp`` extra.
    if rest and rest[0] == "--setup":
        from mini_ork.acp.setup import run_interactive

        return run_interactive(Path.cwd())
    if rest:
        sys.stderr.write(f"mini-ork acp: unexpected argument: {rest[0]}\n")
        sys.stderr.write(_USAGE)
        return 2
    try:
        import acp  # noqa: F401
    except ImportError:
        sys.stderr.write("mini-ork acp: the optional 'acp' extra is not installed\n")
        sys.stderr.write("  Install with: pip install -e '.[acp]'\n")
        return 1

    from acp import Agent

    from mini_ork.acp.agent import MiniOrkAcpAgent

    # MiniOrkAcpAgent implements the slice-0 subset of the Agent protocol
    # (initialize/new_session/prompt); the router only routes what it has.
    asyncio.run(acp.run_agent(cast(Agent, MiniOrkAcpAgent())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", "")))