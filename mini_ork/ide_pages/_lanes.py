"""Shared resolvers for the IDE pages: recipe dir, workflow.yaml, lane map.

Houses the helpers ``mini_ork.ide_pages.dags`` and ``mini_ork.ide_pages.run``
share so a project-overlay recipe resolves the same way on both pages.
``run.py`` owns ``_recipe_dir`` historically; lifting the resolution here
gives the threads page a home-aware read of ``workflow.yaml`` without
having to fork ``mini_ork.web.recipes.fingerprint`` (which uses
``mini_ork_root()/recipes/<name>`` and ignores ``home``).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

# ``mini_ork.ide_pages`` → ``mini_ork`` → engine root. Mirrors ``run.py:38``;
# intentionally does NOT honour ``MINI_ORK_ROOT`` because this module is
# read-only display and the engine tree is the only authoritative source.
_ENGINE_ROOT = Path(__file__).resolve().parents[2]


def _yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:  # PyYAML is optional; degrade silently.
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a typo in a config must not break the tab
        return {}
    return data if isinstance(data, dict) else {}


def _cached_yaml(path_str: str) -> dict[str, Any]:
    """Memoise YAML reads keyed on the file's absolute path **and mtime**.

    The same ``<home>/config/agents.yaml`` and ``<engine>/config/agents.yaml``
    are read on every ``lane_map`` call; caching keeps a 50-run board poll
    to one disk read per path instead of up to 3 × 50.

    The mtime is part of the key rather than the path alone, because the cache
    outlives a single call in any long-lived host (the web server, the Python
    SDK, a REPL). Re-pointing ``<home>/config/agents.yaml`` mid-session — what
    a lane re-point does — must not keep serving the old labels until 64
    other paths happen to evict the entry. A ``stat`` is far cheaper than the
    read it saves, and the IDE's own path (one ``mini-ork board page`` process
    per poll) never sees a stale entry either way.
    """
    try:
        mtime_ns = Path(path_str).stat().st_mtime_ns
    except OSError:  # a missing file already reads as {} below
        mtime_ns = 0
    return _cached_yaml_at(path_str, mtime_ns)


@lru_cache(maxsize=64)
def _cached_yaml_at(path_str: str, mtime_ns: int) -> dict[str, Any]:
    """The parse, cached on ``(path, mtime_ns)``.

    ``mtime_ns`` takes no part in the parse — it is in the signature so that an
    edited file is a different key and re-reads.
    """
    del mtime_ns  # part of the key only; see ``_cached_yaml``
    return _yaml(Path(path_str))


def recipe_dir(home: Path, name: str) -> Path | None:
    """Resolve a recipe directory home-aware (project overlay wins over engine).

    Mirrors ``mini_ork.ide_pages.run._recipe_dir`` so the dags page and the
    run page agree on which ``workflow.yaml`` a given run actually used.
    Returns ``None`` for an empty name or any failure (incl. a missing
    ``recipes_catalog``); the caller surfaces that as ``errors[run_id]``.
    """
    if not name:
        return None
    try:
        from mini_ork.recipes_catalog import find_recipe
        info = find_recipe(name, home)
    except Exception:  # noqa: BLE001 — surface in errors, not on stdout
        return None
    return info.path if info is not None else None


def workflow_yaml(home: Path, name: str) -> dict[str, Any]:
    """Home-aware read of a recipe's ``workflow.yaml``.

    Empty ``{}`` for any failure (typo, missing dir, YAML parse error) so
    the dags page can record a per-run ``"recipe not found"`` without
    aborting the rest of the batch.
    """
    path = recipe_dir(home, name)
    if path is None:
        return {}
    return _cached_yaml(str(path / "workflow.yaml"))


def lane_map(home: Path, run_dir: Path) -> dict[str, str]:
    """role key → first provider lane, from the run's agents.yaml snapshot.

    Resolution order (first file with a non-empty ``lanes`` block wins):

    1. ``<run_dir>/config/agents.yaml`` — the per-run snapshot, so a
       re-pointed lane shows the model that actually served the run.
    2. ``<home>/config/agents.yaml`` — the project's overlay.
    3. ``<engine root>/config/agents.yaml`` — the team template.

    Each ``role: "a,b,c"`` chain is split on ``,`` and the first non-empty
    entry is kept. A role whose first lane is empty is omitted.

    Home and engine reads are cached (lru_cache on the path string), so
    the per-run snapshot is the only file we re-read each call.
    """
    for path in (
        run_dir / "config" / "agents.yaml",
        home / "config" / "agents.yaml",
        _ENGINE_ROOT / "config" / "agents.yaml",
    ):
        lanes = _cached_yaml(str(path)).get("lanes")
        if isinstance(lanes, dict) and lanes:
            out: dict[str, str] = {}
            for role, chain in lanes.items():
                first = str(chain or "").split(",")[0].strip()
                if first:
                    out[str(role)] = first
            return out
    return {}