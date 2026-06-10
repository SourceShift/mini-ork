"""Read-only observability HTTP surface for a local mini-ork project.

Boots a FastAPI app that opens .mini-ork/state.db with PRAGMA query_only,
serves REST endpoints + SSE event stream, and (when bundled) static React
assets from mini_ork/web/static/.

Run via: mini-ork serve [--port 7090] [--home .mini-ork]
"""

from .app import create_app

__all__ = ["create_app"]
