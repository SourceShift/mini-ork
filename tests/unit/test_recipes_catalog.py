"""Hermetic tests for ``mini_ork.recipes_catalog``.

The catalog is the single resolver for project + engine recipes; the
picker (ACP ``MiniOrkAcpAgent._list_recipes``) and the MCP
``list_recipes`` tool both consume it. These tests pin every
documented behaviour:

* engine-only: home with no ``recipes/`` → engine entries only
* project-only: empty engine → project entries only
* project shadows engine: same id → one entry, source=project, shadows_engine=True
* home == engine recipes dir: no duplicate listings
* directory missing ``task_class.yaml`` → skipped
* broken YAML → listed with empty description, ``node_count=0``
* description truncation at 120 chars (when no fallback)
* ``find_recipe`` hit and miss
* ``home=None`` → engine only, no project scan

The engine root is patched via ``mini_ork.web.control._mini_ork_root`` so
no real engine checkout is needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork import recipes_catalog  # noqa: E402
from mini_ork.recipes_catalog import RecipeInfo  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────


def _write_recipe(
    recipes_dir: Path,
    name: str,
    *,
    description: str = "",
    task_class_name: str = "",
    nodes: int = 0,
    task_class_yaml: str | None = None,
    workflow_yaml: str | None = None,
    include_task_class: bool = True,
) -> Path:
    """Materialise one recipe directory; return its path.

    ``description`` populates ``task_class.yaml``'s description; an empty
    value writes a task_class.yaml without it. ``nodes`` is the number
    of items written to ``workflow.yaml``'s ``nodes`` list.
    """
    d = recipes_dir / name
    d.mkdir(parents=True, exist_ok=True)
    if include_task_class:
        if task_class_yaml is None:
            lines = [f"name: {task_class_name or name}"]
            if description:
                lines.append(f"description: {description}")
            (d / "task_class.yaml").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
        else:
            (d / "task_class.yaml").write_text(task_class_yaml, encoding="utf-8")
    if workflow_yaml is None:
        if nodes > 0:
            node_lines = "\n".join(f"  - id: n{i}\n    type: verifier" for i in range(nodes))
            (d / "workflow.yaml").write_text(
                f"name: {name}\nnodes:\n{node_lines}\n", encoding="utf-8"
            )
        else:
            (d / "workflow.yaml").write_text(f"name: {name}\n", encoding="utf-8")
    else:
        (d / "workflow.yaml").write_text(workflow_yaml, encoding="utf-8")
    return d


def _patch_engine(monkeypatch, engine_root: Path) -> None:
    """Point ``mini_ork.web.control._mini_ork_root`` at ``engine_root``."""
    monkeypatch.setattr(
        "mini_ork.web.control._mini_ork_root", lambda: engine_root
    )


# ── cases ────────────────────────────────────────────────────────────────


def test_engine_only(monkeypatch, tmp_path):
    """Home has no ``recipes/``; engine has two recipes → engine only."""
    engine = tmp_path / "engine"
    engine.mkdir()
    engine_recipes = engine / "recipes"
    _write_recipe(engine_recipes, "alpha", description="Alpha", nodes=2)
    _write_recipe(engine_recipes, "beta", description="Beta", nodes=1)
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"  # no recipes/ under it
    home.mkdir(parents=True)
    out = recipes_catalog.list_recipes(home)
    assert [e.id for e in out] == ["alpha", "beta"]
    assert all(e.source == "engine" for e in out)
    assert all(e.shadows_engine is False for e in out)
    by_id = {e.id: e for e in out}
    assert by_id["alpha"].description == "Alpha"
    assert by_id["alpha"].node_count == 2
    assert by_id["beta"].node_count == 1


def test_project_only(monkeypatch, tmp_path):
    """Engine has no ``recipes/``; project has two recipes → project only."""
    engine = tmp_path / "engine"
    engine.mkdir()  # no recipes/ under it
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    project_recipes = home / "recipes"
    _write_recipe(project_recipes, "my-audit", description="My audit", nodes=1)
    _write_recipe(project_recipes, "my-fix", description="My fix", nodes=0)
    out = recipes_catalog.list_recipes(home)
    assert [e.id for e in out] == ["my-audit", "my-fix"]
    assert all(e.source == "project" for e in out)
    assert all(e.shadows_engine is False for e in out)


def test_project_shadows_engine(monkeypatch, tmp_path):
    """Same id under both → one entry, source=project, shadows_engine=True."""
    engine = tmp_path / "engine"
    engine.mkdir()
    engine_recipes = engine / "recipes"
    _write_recipe(engine_recipes, "code-fix", description="Engine code-fix", nodes=3)
    _write_recipe(engine_recipes, "alpha", description="Engine alpha", nodes=1)
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    project_recipes = home / "recipes"
    _write_recipe(project_recipes, "code-fix", description="Project code-fix", nodes=5)
    out = recipes_catalog.list_recipes(home)
    by_id = {e.id: e for e in out}
    assert set(by_id) == {"alpha", "code-fix"}
    # The shadowed entry: project wins, marks shadows_engine=True.
    assert by_id["code-fix"].source == "project"
    assert by_id["code-fix"].shadows_engine is True
    assert by_id["code-fix"].description == "Project code-fix"
    assert by_id["code-fix"].node_count == 5
    # The non-shadowed engine entry is normal.
    assert by_id["alpha"].source == "engine"
    assert by_id["alpha"].shadows_engine is False


def test_home_eq_engine_recipes_dir_no_duplicates(monkeypatch, tmp_path):
    """When ``<home>/recipes`` resolves to the engine's ``recipes/``,
    each recipe is listed once as ``engine`` (no duplicates)."""
    engine = tmp_path / "engine"
    engine.mkdir()
    engine_recipes = engine / "recipes"
    _write_recipe(engine_recipes, "alpha", description="Alpha")
    _write_recipe(engine_recipes, "beta", description="Beta")
    _patch_engine(monkeypatch, engine)

    # Make ``<home>/recipes`` a SYMLINK to ``<engine>/recipes`` so
    # ``Path.resolve()`` collapses both. ``engine_recipes`` already
    # exists on disk, so a symlink chain works.
    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    (home / "recipes").symlink_to(engine_recipes)

    out = recipes_catalog.list_recipes(home)
    ids = [e.id for e in out]
    assert sorted(ids) == ["alpha", "beta"]
    assert all(e.source == "engine" for e in out)
    assert all(e.shadows_engine is False for e in out)


def test_directory_missing_task_class_yaml_skipped(monkeypatch, tmp_path):
    """Half-baked recipe (no ``task_class.yaml``) is not listed."""
    engine = tmp_path / "engine"
    engine.mkdir()
    engine_recipes = engine / "recipes"
    _write_recipe(engine_recipes, "alpha", description="Alpha")
    # Missing task_class.yaml
    half = engine_recipes / "half"
    half.mkdir(parents=True, exist_ok=True)
    (half / "workflow.yaml").write_text("name: half\n", encoding="utf-8")
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    out = recipes_catalog.list_recipes(home)
    assert [e.id for e in out] == ["alpha"]


def test_broken_yaml_listed_with_defaults(monkeypatch, tmp_path):
    """A recipe whose YAML doesn't parse is still listed with empty
    description, empty task_class, and ``node_count=0``."""
    engine = tmp_path / "engine"
    engine.mkdir()
    engine_recipes = engine / "recipes"
    broken = engine_recipes / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "task_class.yaml").write_text(
        "name: broken\ndescription: 'oops\n: not yaml", encoding="utf-8"
    )
    (broken / "workflow.yaml").write_text(
        "name: broken\nnodes: [\n  - this is not a list",
        encoding="utf-8",
    )
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    out = recipes_catalog.list_recipes(home)
    assert len(out) == 1
    entry = out[0]
    assert entry.id == "broken"
    assert entry.source == "engine"
    assert entry.description == ""
    assert entry.task_class == ""
    assert entry.node_count == 0
    assert entry.shadows_engine is False


def test_description_truncated_at_120_chars(monkeypatch, tmp_path):
    """A long description is truncated to 120 chars on the first line;
    the workflow.yaml description is the fallback when the task_class
    one is missing or empty."""
    engine = tmp_path / "engine"
    engine.mkdir()
    engine_recipes = engine / "recipes"
    long_desc = "x" * 200
    _write_recipe(engine_recipes, "long", description=long_desc)
    # Fallback: only workflow.yaml has a description.
    _write_recipe(
        engine_recipes,
        "fallback",
        description="",
        workflow_yaml="name: fallback\ndescription: wf-only desc\nnodes: [a]\n",
    )
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    out = recipes_catalog.list_recipes(home)
    by_id = {e.id: e for e in out}
    assert len(by_id["long"].description) == 120
    assert by_id["long"].description == "x" * 120
    assert by_id["fallback"].description == "wf-only desc"


def test_find_recipe_hit_and_miss(monkeypatch, tmp_path):
    """``find_recipe`` returns the entry when present, None otherwise."""
    engine = tmp_path / "engine"
    engine.mkdir()
    engine_recipes = engine / "recipes"
    _write_recipe(engine_recipes, "alpha", description="Alpha")
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    hit = recipes_catalog.find_recipe("alpha", home)
    assert hit is not None
    assert hit.id == "alpha"
    assert hit.source == "engine"
    miss = recipes_catalog.find_recipe("does-not-exist", home)
    assert miss is None


def test_home_none_lists_engine_only(monkeypatch, tmp_path):
    """``home=None`` skips the project scan; engine entries are listed."""
    engine = tmp_path / "engine"
    engine.mkdir()
    engine_recipes = engine / "recipes"
    _write_recipe(engine_recipes, "alpha", description="Alpha")
    _write_recipe(engine_recipes, "beta", description="Beta")
    _patch_engine(monkeypatch, engine)

    out = recipes_catalog.list_recipes(None)
    assert [e.id for e in out] == ["alpha", "beta"]
    assert all(e.source == "engine" for e in out)


# ── defensive cases ─────────────────────────────────────────────────────


def test_engine_root_unresolvable_returns_empty(monkeypatch, tmp_path):
    """When ``_mini_ork_root`` raises, ``list_recipes`` returns ``[]``."""

    def _boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", _boom)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    assert recipes_catalog.list_recipes(home) == []
    assert recipes_catalog.find_recipe("anything", home) is None


def test_recipe_info_is_frozen_dataclass():
    """Sanity: ``RecipeInfo`` is frozen (kickoff contract)."""
    entry = RecipeInfo(
        id="x",
        source="engine",
        path=Path("/_x"),
        description="",
        task_class="",
        node_count=0,
        shadows_engine=False,
    )
    with pytest.raises((AttributeError, Exception)):
        entry.id = "y"  # type: ignore[misc]
