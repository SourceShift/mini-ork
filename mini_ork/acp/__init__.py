"""mini-ork ACP (Agent Client Protocol) stdio agent.

Slice 0 of the Zed engineer surface: a stdio ACP server where one ACP session
is one mini-ork run — the session id *is* the run id, so there is no
session→run mapping table. The agent is registered as ``mini-ork acp`` and
speaks the protocol over stdin/stdout via the official ``agent-client-protocol``
SDK.

This package is the only place that imports ``acp`` (an optional extra), so the
core runtime stays dependency-free.
"""
from __future__ import annotations

from mini_ork.acp.agent import MiniOrkAcpAgent, mint_run_id

__all__ = ["MiniOrkAcpAgent", "mint_run_id"]
