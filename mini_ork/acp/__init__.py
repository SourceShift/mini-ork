"""mini-ork ACP (Agent Client Protocol) stdio agent.

Slice 0 of the Zed engineer surface: a stdio ACP server where one ACP session
is one mini-ork run — the session id *is* the run id, so there is no
session→run mapping table. The agent is registered as ``mini-ork acp`` and
speaks the protocol over stdin/stdout via the official ``agent-client-protocol``
SDK.

This package is the only place that imports ``acp`` (an optional extra), so the
core runtime stays dependency-free.

The agent itself is loaded lazily via module ``__getattr__`` so
``import mini_ork.acp.fleet`` (a hot path on every ``board --json``) does not
pay for importing ``acp`` and the ACP SDK. Touching ``MiniOrkAcpAgent`` or
``mint_run_id`` resolves them on first access; a missing ``acp`` extra raises
``ImportError`` there, not at every IDE poll.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["MiniOrkAcpAgent", "mint_run_id"]


def __getattr__(name: str) -> Any:
    if name in {"MiniOrkAcpAgent", "mint_run_id"}:
        from mini_ork.acp import agent as _agent

        value = getattr(_agent, name)
        globals()[name] = value  # cache for subsequent attribute access
        return value
    raise AttributeError(f"module 'mini_ork.acp' has no attribute {name!r}")


if TYPE_CHECKING:  # pragma: no cover — import-only for type checkers
    from mini_ork.acp.agent import MiniOrkAcpAgent, mint_run_id
