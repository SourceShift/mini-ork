"""Recipe discovery across the project home and the engine root.

The catalog is the single source of truth for what recipes a thread session
can pick, and what the ``list_recipes`` MCP tool returns. It mirrors the
run-time resolution in :func:`mini_ork.cli.main._resolve_recipe_base`:

1. ``<home>/recipes/<id>/`` (the project overlay; user-authored recipes)
2. ``<engine_root>/recipes/<id>/`` (the bundled recipes that ship with
   ``mini-ork``; engine root is
   :func:`mini_ork.web.control._mini_ork_root`)

A project recipe wins on a name clash — the engine recipe is omitted and
the project entry carries ``shadows_engine=True``. When the two
``recipes/`` directories resolve to the same real path (a dev checkout
where ``home`` IS the engine, or a symlink overlay) every recipe is
listed once as ``"engine"`` so the picker never shows duplicates.

YAML errors never raise. A recipe with an unparseable
``task_class.yaml`` or ``workflow.yaml`` is still listed with empty
description, empty ``task_class``, ``node_count=0`` — the picker stays
usable even when one recipe is broken.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # PyYAML is a soft dep of recipe authoring
    yaml = None  # type: ignore[assignment]


@dataclass(frozen=True)
class RecipeInfo:
    """A single recipe entry the picker / MCP tool can show.

    Attributes:
        id: directory name (``<root>/recipes/<id>/``).
        source: ``"project"`` (under ``<home>/recipes/``) or ``"engine"``
            (under ``<engine_root>/recipes/``).
        path: the recipe directory (used by the launcher to read its
            files; the picker does not need it).
        description: first line of ``task_class.yaml``'s ``description``
            field (truncated to 120 chars), falling back to
            ``workflow.yaml``'s ``description``, then ``""`` when neither
            is set.
        task_class: ``task_class.yaml``'s ``name`` field, or ``""``.
        node_count: ``len(workflow.yaml`` ``nodes``)``; 0 when unreadable.
        shadows_engine: True ONLY for a project recipe that hides an
            engine recipe with the same id; the engine entry is omitted
            in that case.
    """

    id: str
    source: str
    path: Path
    description: str
    task_class: str
    node_count: int
    shadows_engine: bool


# ── YAML + recipe-dir predicates (no raise) ───────────────────────────────


def _safe_yaml(path: Path) -> dict[str, Any]:
    """Best-effort YAML load — returns ``{}`` on any failure.

    Mirrors :func:`mini_ork.web.recipes._safe_load` so a typo in a
    user's recipe does not crash the picker.
    """
    if not path.exists() or yaml is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _first_line(text: Any, *, limit: int = 120) -> str:
    """First non-empty line of ``text``, truncated to ``limit`` chars."""
    if not isinstance(text, str):
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            return line[:limit]
    return ""


def _recipe_dir_is_recipe(path: Path) -> bool:
    """A recipe directory contains BOTH ``workflow.yaml`` and ``task_class.yaml``.

    Mirrors ``mini_ork.mcp_context.server._list_recipes``'s predicate so
    discovery stays consistent across the picker, the MCP tool, and the
    launcher.
    """
    if not path.is_dir():
        return False
    if not (path / "workflow.yaml").exists():
        return False
    if not (path / "task_class.yaml").exists():
        return False
    return True


def _build_entry(
    recipe_dir: Path, *, source: str, shadows_engine: bool
) -> RecipeInfo:
    """Materialise a single ``RecipeInfo`` for ``recipe_dir`` (no raising)."""
    tc = _safe_yaml(recipe_dir / "task_class.yaml")
    wf = _safe_yaml(recipe_dir / "workflow.yaml")
    description = _first_line(tc.get("description")) or _first_line(
        wf.get("description")
    )
    raw_name = tc.get("name")
    task_class = str(raw_name).strip() if isinstance(raw_name, str) else ""
    raw_nodes = wf.get("nodes")
    node_count = len(raw_nodes) if isinstance(raw_nodes, list) else 0
    return RecipeInfo(
        id=recipe_dir.name,
        source=source,
        path=recipe_dir,
        description=description,
        task_class=task_class,
        node_count=node_count,
        shadows_engine=shadows_engine,
    )


# ── public API ─────────────────────────────────────────────────────────────


def list_recipes(home: Path | None) -> list[RecipeInfo]:
    """Discover every recipe visible to a thread session rooted at ``home``.

    Search order: ``<home>/recipes/`` (project), then
    ``<engine_root>/recipes/`` (engine). A project recipe with the same
    id as an engine recipe wins; the engine one is omitted. When the two
    ``recipes/`` directories resolve to the same real path, every entry
    is listed once as ``"engine"``.

    ``home=None`` skips the project scan entirely; only the engine is
    listed. Returned entries are sorted by id.

    Never raises: a missing engine root, a missing home, an unreadable
    ``recipes/`` directory, or a YAML error all degrade to the entries
    that can be read.
    """
    try:
        from mini_ork.web.control import _mini_ork_root

        engine_root = _mini_ork_root()
    except Exception:
        return []
    engine_recipes_dir = engine_root / "recipes"

    project_recipes_dir: Path | None = None
    if home is not None:
        project_recipes_dir = home / "recipes"

    # Same-directory dedup: a dev checkout where home is the engine
    # (or where the project symlinks to the engine's recipes dir) must
    # list each recipe once, as ``engine``. Use ``Path.resolve()`` so
    # symlinks collapse; ``Path.samefile()`` would raise on a broken
    # symlink.
    same_dir = False
    if project_recipes_dir is not None:
        try:
            same_dir = (
                project_recipes_dir.resolve() == engine_recipes_dir.resolve()
            )
        except OSError:
            same_dir = False
    if same_dir:
        project_recipes_dir = None

    # Engine ids (needed to mark project recipes as shadowing).
    engine_ids: set[str] = set()
    if engine_recipes_dir.is_dir():
        try:
            for p in engine_recipes_dir.iterdir():
                if _recipe_dir_is_recipe(p):
                    engine_ids.add(p.name)
        except OSError:
            pass

    entries: list[RecipeInfo] = []
    seen_ids: set[str] = set()

    # 1. Project recipes (skipped when same_dir collapsed the two dirs).
    if project_recipes_dir is not None and project_recipes_dir.is_dir():
        try:
            children = sorted(
                p for p in project_recipes_dir.iterdir() if _recipe_dir_is_recipe(p)
            )
        except OSError:
            children = []
        for p in children:
            shadows = p.name in engine_ids
            entries.append(
                _build_entry(p, source="project", shadows_engine=shadows)
            )
            seen_ids.add(p.name)

    # 2. Engine recipes (omit any id already covered by a project entry).
    if engine_recipes_dir.is_dir():
        try:
            children = sorted(
                p for p in engine_recipes_dir.iterdir() if _recipe_dir_is_recipe(p)
            )
        except OSError:
            children = []
        for p in children:
            if p.name in seen_ids:
                continue
            entries.append(_build_entry(p, source="engine", shadows_engine=False))
            seen_ids.add(p.name)

    entries.sort(key=lambda e: e.id)
    return entries


def find_recipe(recipe_id: str, home: Path | None) -> RecipeInfo | None:
    """Return the entry with id ``recipe_id`` or ``None`` if absent.

    Uses the same resolution as :func:`list_recipes` — a project entry
    with the same id shadows the engine's.
    """
    for entry in list_recipes(home):
        if entry.id == recipe_id:
            return entry
    return None
