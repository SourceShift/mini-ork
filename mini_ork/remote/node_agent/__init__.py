"""Node-agent HTTP server — turns a VM into a mini-ork data-plane host.

See :mod:`mini_ork.remote` for the package rationale. The FastAPI factory
:func:`create_app` is the single entrypoint; the CLI wrapper
:mod:`mini_ork.cli.node_agent` binds it to uvicorn with the launcher-level
bind / TLS / token-env validation the kickoff requires.
"""
from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]