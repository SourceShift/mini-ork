"""FastAPI app factory for the mini-ork observability UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .deps import get_db, get_home, set_home_override
from .routes import (
    control as control_routes,
    fingerprint,
    fleet,
    projects,
    run_detail,
    stream,
    trajectory,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(home: Path | None = None, dev_cors: bool = True) -> FastAPI:
    """Build the FastAPI app.

    Args:
      home: optional override for .mini-ork home (else $MINI_ORK_HOME or cwd/.mini-ork)
      dev_cors: when True, allow http://localhost:5173 (Vite dev server) for CORS.
    """
    if home is not None:
        set_home_override(home)

    app = FastAPI(
        title="mini-ork observability",
        version="0.1.0",
        description="Read-only HTTP surface over .mini-ork/state.db + runs/",
    )

    if dev_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:7070",
                "http://127.0.0.1:7070",
                # Keep old Vite default to ease transition for users who alias
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            ],
            allow_credentials=False,
            # POST is needed for stop/kill control endpoints
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    app.include_router(fleet.router)
    app.include_router(run_detail.router)
    app.include_router(trajectory.router)
    app.include_router(fingerprint.router)
    app.include_router(stream.router)
    app.include_router(control_routes.router)
    app.include_router(projects.router)

    @app.get("/api")
    def api_index() -> JSONResponse:
        # Derive endpoints from app.routes so the index never drifts from
        # reality. Filter to /api/v1 only and skip internal FastAPI routes.
        endpoints = sorted(
            r.path  # type: ignore[attr-defined]
            for r in app.routes
            if getattr(r, "path", "").startswith("/api/v1")
        )
        return JSONResponse(
            {
                "name": "mini-ork-observability",
                "version": "0.1.0",
                "home": str(get_home()),
                "db": str(get_db().db_path),
                "endpoint_count": len(endpoints),
                "endpoints": endpoints,
            }
        )

    # SPA: serve built React bundle when present. Falls back to API-only mode
    # during early dev when web/dist hasn't been built yet.
    if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(STATIC_DIR / "assets")),
            name="assets",
        )

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")
    else:

        @app.get("/")
        def root() -> JSONResponse:
            return JSONResponse(
                {
                    "message": "mini-ork observability API is running",
                    "ui_built": False,
                    "api_index": "/api",
                    "hint": "Run `pnpm --dir web build` to ship the SPA bundle, "
                    "or `pnpm --dir web dev` for live React dev server on :5173",
                }
            )

    return app


# uvicorn entrypoint: `uvicorn mini_ork.web.app:app --port 7090`
app = create_app()
