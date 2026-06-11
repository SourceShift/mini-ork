"""Read-only observability HTTP surface for a local mini-ork project.

Boots a FastAPI app that opens .mini-ork/state.db with PRAGMA query_only,
serves REST endpoints + SSE event stream, and (when bundled) static React
assets from mini_ork/web/static/.

Run via: mini-ork serve [--port 7090] [--home .mini-ork]
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(name)
