"""FastAPI app factory for the mini-ork observability UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .deps import get_db, get_home, set_home_override
from .routes import (
    agent_server,
    artifacts as artifacts_routes,
    control as control_routes,
    dispatch as dispatch_routes,
    fingerprint,
    fleet,
    idea_tree as idea_tree_routes,
    learning,
    projects,
    pty as pty_routes,
    recovery as recovery_routes,
    run_detail,
    runs as runs_routes,
    sockets as sockets_routes,
    stream,
    traceotter,
    trajectory,
    workspace as workspace_routes,
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
                # Electron renderers (Orca) load from file:// and therefore send
                # `Origin: null`. Without this every fetch from an embedded panel is
                # blocked by CORS — and blocked-by-CORS looks exactly like "no data",
                # which is the silent-empty-panel failure we refuse elsewhere.
                "null",
            ],
            # Any page SERVED FROM localhost may call us, on any port — this is what
            # lets an Electron dev renderer (whose Vite port is not fixed) work without
            # hard-coding it. A remote origin like https://evil.com does NOT match, so
            # this does not open the control endpoints to the web at large. The server
            # binds 127.0.0.1 regardless.
            allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
            allow_credentials=False,
            # POST is needed for stop/kill control endpoints
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    app.include_router(fleet.router)
    # run-artifacts (DB registry over run_artifacts). Mounted at
    # /artifact-records so it cannot shadow run_detail's /artifacts
    # filesystem-scan endpoints the SPA consumes.
    app.include_router(artifacts_routes.router)
    app.include_router(run_detail.router)
    app.include_router(trajectory.router)
    app.include_router(fingerprint.router)
    app.include_router(stream.router)
    app.include_router(control_routes.router)
    app.include_router(runs_routes.router)
    app.include_router(projects.router)
    app.include_router(recovery_routes.router)
    app.include_router(idea_tree_routes.router)
    app.include_router(learning.router)
    app.include_router(traceotter.router)
    app.include_router(dispatch_routes.router)
    # WebSocket PTY bridge (opt-in via MO_PTY_ENABLED=1). Registered before the
    # SPA catch-all so `/api/v1/pty` is never shadowed by the index.html fallback.
    app.include_router(pty_routes.router)
    # OpenHands agent-server protocol shim (SE-3 UI fork). Serves /server_info,
    # /api/settings, /alive, /health, /ready so the forked agent-canvas SPA
    # probes green and completes onboarding. Registered before the SPA catch-all
    # so these exact paths aren't swallowed by the index.html fallback.
    app.include_router(agent_server.router)
    # The workspace panel's vocabulary — files, git, and the bash runtime.
    # `execute_bash_command` is how the Files tab enumerates the tree, and the
    # git routes back the changes/diff/commits tabs; without them those tabs
    # are empty rather than absent. Also registered before the SPA catch-all.
    app.include_router(workspace_routes.router)
    # Canvas event WebSocket (SE-3 UI fork). `/sockets/events/{id}` is the
    # socket the canvas opens per conversation and falls back to REST polling
    # without — the "Disconnected" chip. `sockets` is already reserved in
    # _NON_SPA_PREFIXES, so the upgrade never reaches the index.html fallback.
    app.include_router(sockets_routes.router)

    @app.get("/api")
    def api_index() -> JSONResponse:
        # Derive the endpoint list from the resolved OpenAPI schema so the index
        # never drifts from reality. It used to walk `app.routes`, which stopped
        # working in FastAPI 0.139: `include_router` now appends a lazy
        # `_IncludedRouter` whose `.path` is None, so every router the app mounts
        # was invisible and the index advertised `endpoint_count: 0` while the
        # server served 101 paths. A wrong 200 that under-reports the surface is
        # the same failure mode the 404-vs-405 note below guards against — the
        # reader concludes "nothing is implemented" from a response that parsed
        # fine. `app.openapi()` resolves the lazy wrappers (and is cached).
        paths = sorted(app.openapi().get("paths", {}))
        # `endpoints` stays the /api/v1 family: the observability API this index
        # is named for. The agent-server shim (the canvas's wire protocol) lives
        # beside it under /api/* and is counted separately rather than hidden.
        endpoints = [p for p in paths if p.startswith("/api/v1")]
        agent_server = [p for p in paths if p.startswith("/api/") and not p.startswith("/api/v1")]
        return JSONResponse(
            {
                "name": "mini-ork-observability",
                "version": "0.1.0",
                "home": str(get_home()),
                "db": str(get_db().db_path),
                "endpoint_count": len(endpoints),
                "endpoints": endpoints,
                "agent_server_endpoint_count": len(agent_server),
                "agent_server_endpoints": agent_server,
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

        # Prefixes that are wire-protocol surfaces, never client-side SPA
        # routes. Anything reaching the catch-all under one of these was NOT
        # matched by a real router, i.e. it is genuinely unimplemented — so we
        # answer 404 JSON. Serving index.html (HTML, status 200) here is the
        # silent-false-success trap: an SDK client parses HTML as JSON, fails
        # opaquely, and the failure looks like "no data" rather than "not
        # implemented". Same refusal-of-silent-empty posture as the CORS note.
        _NON_SPA_PREFIXES = (
            "api/",
            "server_info",
            "alive",
            "health",
            "ready",
            "sockets",
        )

        # response_model=None: the union return (FileResponse | JSONResponse)
        # is not a Pydantic-derivable type, so we opt out of response-model
        # generation rather than let FastAPI try to build a schema from it.
        #
        # Registered for every method, not GET alone, because a GET-only
        # catch-all answers every *unmatched write* with 405 — and the canvas
        # reads 405 as a hard failure it surfaces as an error toast, whereas it
        # reads 404 as "this server predates the endpoint" and hides the panel
        # quietly (see agent-server-git-service.api.ts). So the mismatch was
        # inverted: the paths the shim does not implement were the loud ones,
        # and a toast for a feature the canvas would otherwise have hidden is a
        # worse answer than the honest 404. Anything that is not a GET lands on
        # the 404 branch below.
        @app.api_route(
            "/{full_path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            response_model=None,
            # A fallback has no useful schema, and a five-method route makes
            # FastAPI 0.139 emit the same operationId for all five
            # (`spa_fallback__full_path__put`) — six warnings on every
            # `app.openapi()` call, which the /api index now makes. Keeping it
            # out of the schema removes both problems: OpenAPI stops warning,
            # and the docs stop advertising a catch-all as a real endpoint.
            include_in_schema=False,
        )
        def spa_fallback(request: Request, full_path: str) -> FileResponse | JSONResponse:
            if request.method != "GET" or full_path.startswith(_NON_SPA_PREFIXES):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            # A real file shipped beside index.html — locales/<lng>/openhands.json,
            # favicon.svg, mockServiceWorker.js, … — must beat the SPA fallback.
            # This is the same silent-false-success trap as the 404 above, one
            # step worse: Vite serves ui/public/ at the dev root, but the built
            # bundle is served from here and only /assets was mounted, so every
            # other root-level asset used to come back as index.html with status
            # 200. The i18n loader fetches /locales/en/openhands.json and parses
            # it as JSON; getting HTML made every string in the UI render as its
            # raw key while every request still reported success.
            #
            # resolve() before the containment check is what makes this safe:
            # a traversal like ../../etc/passwd resolves OUTSIDE STATIC_DIR and
            # is rejected, falling through to index.html.
            candidate = (STATIC_DIR / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(STATIC_DIR.resolve()):
                return FileResponse(candidate)
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
