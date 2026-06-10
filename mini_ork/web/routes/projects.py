"""Project (home) registry + switcher.

The UI serves exactly one .mini-ork home at a time; these endpoints let the
operator hop between project folders without restarting the server. The
registry of known homes persists at ~/.config/mini-ork/projects.json
(override: MINI_ORK_PROJECTS_FILE) so it is shared across serve instances
and survives restarts.

Write boundary note: switch mutates server-side state (the active home) but
never the databases themselves — every home is still opened read-only.
Paths are validated to contain a state.db before being accepted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..deps import get_home_lenient, set_home_override

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _registry_path() -> Path:
    env = os.environ.get("MINI_ORK_PROJECTS_FILE")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "mini-ork" / "projects.json"


def _load_registry() -> list[str]:
    path = _registry_path()
    try:
        data = json.loads(path.read_text())
        homes = data.get("projects", [])
        return [h for h in homes if isinstance(h, str)]
    except (OSError, json.JSONDecodeError):
        return []


def _save_registry(homes: list[str]) -> None:
    path = _registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"projects": homes}, indent=2) + "\n")
    except OSError:
        pass  # registry is a convenience cache; never fail a request over it


def _resolve(home: str) -> Path:
    return Path(home).expanduser().resolve()


def _entry(home: Path, active: Path) -> dict[str, Any]:
    return {
        "home": str(home),
        # ~/ps/researcher/.mini-ork -> "researcher"
        "name": home.parent.name or str(home),
        "exists": (home / "state.db").exists(),
        "active": home == active,
    }


@router.get("")
def list_projects(active: Path = Depends(get_home_lenient)) -> dict[str, Any]:
    homes = [_resolve(h) for h in _load_registry()]
    if active not in homes:
        # Persist, not just display: otherwise the boot home silently
        # vanishes from the list the moment the user switches away.
        homes.insert(0, active)
        _save_registry([str(h) for h in homes])
    return {
        "active": str(active),
        "projects": [_entry(h, active) for h in homes],
    }


@router.get("/browse")
def browse_dirs(path: str = "~") -> dict[str, Any]:
    """Server-side folder listing for the UI's folder picker. Browsers can't
    hand a local absolute path to a web app, so the picker walks the
    filesystem through this endpoint instead (loopback-only server)."""
    base = _resolve(path or "~")
    if not base.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {base}")
    dirs: list[dict[str, Any]] = []
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"permission denied: {base}")
    for child in children:
        if child.name.startswith(".") or not child.is_dir():
            continue
        try:
            is_project = (child / ".mini-ork" / "state.db").exists()
        except PermissionError:
            is_project = False
        dirs.append({"name": child.name, "path": str(child), "is_project": is_project})
    return {
        "path": str(base),
        "parent": str(base.parent) if base != base.parent else None,
        "is_project": (base / ".mini-ork" / "state.db").exists() or (base / "state.db").exists(),
        "dirs": dirs,
    }


def _normalize(raw: str) -> Path:
    """Resolve user input to a .mini-ork home. Accepts either the project
    folder (~/ps/researcher) or the home itself (~/ps/researcher/.mini-ork).
    Raises HTTPException when no state.db can be found."""
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="path required")
    home = _resolve(raw)
    # Prefer <folder>/.mini-ork over a state.db directly in the folder —
    # stray state.db files at project roots (test debris) must not win
    # over the real home.
    if (home / ".mini-ork" / "state.db").exists():
        home = home / ".mini-ork"
    if not (home / "state.db").exists():
        hint = (
            "folder not found"
            if not home.exists() and not home.parent.exists()
            else "no state.db — run `mini-ork init` in that folder first"
        )
        raise HTTPException(status_code=400, detail=f"{home}: {hint}")
    return home


def _register(home: Path) -> None:
    homes = [_resolve(h) for h in _load_registry()]
    if home not in homes:
        homes.append(home)
        _save_registry([str(h) for h in homes])


@router.get("/validate")
def validate_project(path: str = "", active: Path = Depends(get_home_lenient)) -> dict[str, Any]:
    """Dry-run of add/switch for as-you-type UI feedback. Never raises —
    returns {ok: false, error} instead so the input can show inline hints."""
    try:
        home = _normalize(path)
    except HTTPException as e:
        return {"ok": False, "error": str(e.detail)}
    registered = home in [_resolve(h) for h in _load_registry()] or home == active
    return {"ok": True, **_entry(home, active), "registered": registered}


@router.post("/add")
def add_project(body: dict = Body(...), active: Path = Depends(get_home_lenient)) -> dict[str, Any]:
    home = _normalize(body.get("home") or "")
    _register(home)
    return {"ok": True, "project": _entry(home, active)}


@router.post("/switch")
def switch_project(body: dict = Body(...)) -> dict[str, Any]:
    home = _normalize(body.get("home") or "")
    set_home_override(home)
    _register(home)
    return {"ok": True, "active": str(home), "name": _entry(home, home)["name"]}


@router.post("/remove")
def remove_project(body: dict = Body(...), active: Path = Depends(get_home_lenient)) -> dict[str, Any]:
    raw = (body.get("home") or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="home path required")
    home = _resolve(raw)
    if home == active:
        raise HTTPException(status_code=409, detail="cannot remove the active project")
    homes = [h for h in (_resolve(x) for x in _load_registry()) if h != home]
    _save_registry([str(h) for h in homes])
    return {"ok": True, "removed": str(home)}
