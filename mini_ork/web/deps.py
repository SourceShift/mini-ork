"""FastAPI dependency providers (read-only DB handle + home dir).

Workspace scoping: every request may name its own .mini-ork home via the
X-Mini-Ork-Home header (normal fetches) or the `home` query param
(EventSource/SSE cannot set headers). Requests without either fall back
to the server-wide default home (boot --home flag, or the last
POST /projects/switch). StateDB handles are cached per home so clients
viewing different projects don't pay connection setup per request.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import Depends, Header, HTTPException, Query

from .db import StateDB, resolve_home

_default_home: Path | None = None
_dbs: dict[Path, StateDB] = {}
_dbs_lock = threading.Lock()


def set_home_override(home: Path) -> None:
    """Set the server-wide default home (boot flag / project switch)."""
    global _default_home
    _default_home = Path(home).resolve()


def get_default_home() -> Path:
    return _default_home or resolve_home(None)


def resolve_workspace_home(raw: str | Path) -> Path:
    """Resolve a project folder or its ``.mini-ork`` directory to one home.

    A valid home is a fixed point: resolving ``project/.mini-ork`` again must
    not reinterpret an unrelated ``project/.mini-ork/.mini-ork`` database as
    the selected workspace.  This matters because the UI validates a project
    path before sending the returned home through the switch endpoint.
    """
    home = Path(raw).expanduser().resolve()
    if home.name == ".mini-ork" and (home / "state.db").exists():
        return home
    project_home = home / ".mini-ork"
    if (project_home / "state.db").exists():
        return project_home
    return home


def _resolve_request_home(raw: str) -> Path:
    home = resolve_workspace_home(raw)
    if not (home / "state.db").exists():
        raise HTTPException(
            status_code=404, detail=f"workspace not found: no state.db under {home}"
        )
    return home


def get_home(
    x_mini_ork_home: str | None = Header(default=None),
    home: str | None = Query(default=None),
) -> Path:
    # When called directly (tests, route bodies) instead of via Depends,
    # the defaults are FieldInfo sentinels, not str — treat as absent.
    raw = x_mini_ork_home if isinstance(x_mini_ork_home, str) else None
    if raw is None and isinstance(home, str):
        raw = home
    if raw and raw.strip():
        return _resolve_request_home(raw.strip())
    return get_default_home()


def get_home_lenient(
    x_mini_ork_home: str | None = Header(default=None),
    home: str | None = Query(default=None),
) -> Path:
    """Like get_home but falls back to the default home instead of 404ing.
    Used by /projects so a stale workspace never locks the user out of the
    switcher (the only way to recover from a bad workspace)."""
    try:
        return get_home(x_mini_ork_home, home)
    except HTTPException:
        return get_default_home()


def db_for(home: Path) -> StateDB:
    with _dbs_lock:
        db = _dbs.get(home)
        if db is None:
            db = StateDB(home / "state.db")
            _dbs[home] = db
        return db


def get_db(home: Path = Depends(get_home)) -> StateDB:
    if not isinstance(home, Path):  # direct call without Depends resolution
        home = get_home()
    return db_for(home)
