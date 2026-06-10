"""FastAPI dependency providers (read-only DB handle + home dir)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .db import StateDB, resolve_home

_home_override: Path | None = None


def set_home_override(home: Path) -> None:
    global _home_override
    _home_override = Path(home).resolve()
    get_home.cache_clear()
    get_db.cache_clear()


@lru_cache(maxsize=1)
def get_home() -> Path:
    return _home_override or resolve_home(None)


@lru_cache(maxsize=1)
def get_db() -> StateDB:
    home = get_home()
    return StateDB(home / "state.db")
