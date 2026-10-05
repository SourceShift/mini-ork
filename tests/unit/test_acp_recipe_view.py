"""Hermetic tests for ``mini_ork.acp.recipe_view``.

The recipe-view projection is the read-model behind ``/recipes`` and
``/recipe`` (Zed S3a) and the MCP ``describe_recipe`` tool. Tests pin:

* row projection across engine + project (incl. shadowing) recipes;
* filter (``source`` + free-text needle) and ordering (project first,
  then by runs desc, then id asc);
* grade computed via ``eval_recipe`` — including a recipe missing
  ``artifact_contract.yaml`` (degrades gracefully);
* track-record numbers (runs, success %, avg cost, avg duration) read
  from a migrated ``state.db``;
* card shape: steps table, flow line, checks column, contract rows,
  keywords, files list;
* unknown recipe id → ``None``;
* describe_recipe_payload returns paths as strings (MCP-friendly).
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import recipe_view  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────


def _write_recipe(
    recipes_dir: Path,
    name: str,
    *,
    description: str = "",
    task_class_name: str = "",
    keywords: tuple[str, ...] = (),
    nodes: list[dict[str, object]] | int | None = None,
    edges: list[dict[str, object]] | None = None,
    artifact: dict[str, object] | None = None,
    include_artifact: bool = True,
    example_kickoff: str | None = None,
    prompts: dict[str, str] | None = None,
    verifiers: dict[str, str] | None = None,
) -> Path:
    """Materialise one recipe directory; return its path.

    ``nodes`` accepts a list of dicts (serialised into ``workflow.yaml`` with
    id/type/model_lane/verifier_ref/gates) or an int (older tests use the
    int as a step count and the resulting YAML just has ``nodes:`` absent
    in the same shape the catalog fixture used). ``edges`` is a list of
    dicts. ``artifact`` is rendered into ``artifact_contract.yaml``; pass
    ``include_artifact=False`` to omit the file entirely (so the grade
    degrades). ``example_kickoff`` writes a flat
    ``<recipe>/example-kickoff.md``; ``prompts`` and ``verifiers`` write
    per-file content under their respective dirs.
    """
    nodes_list: list[dict[str, object]] | None
    if isinstance(nodes, int):
        nodes_list = [
            {"id": f"n{i}", "type": "verifier"} for i in range(nodes)
        ]
    else:
        nodes_list = nodes
    d = recipes_dir / name
    d.mkdir(parents=True, exist_ok=True)
    # task_class.yaml
    tc_lines: list[str] = [f"name: {task_class_name or name}"]
    if description:
        tc_lines.append(f"description: {description}")
    if keywords:
        kw_yaml = ", ".join(f'"{k}"' for k in keywords)
        tc_lines.append("matches:")
        tc_lines.append(f"  keywords: [{kw_yaml}]")
    (d / "task_class.yaml").write_text(
        "\n".join(tc_lines) + "\n", encoding="utf-8"
    )
    # workflow.yaml
    wf_lines: list[str] = [f"name: {name}"]
    if nodes_list is not None:
        wf_lines.append("nodes:")
        for n in nodes_list:
            wf_lines.append(f"  - id: {n.get('id') or 'n'}")
            wf_lines.append(f"    type: {n.get('type') or 'verifier'}")
            if n.get("model_lane"):
                wf_lines.append(f"    model_lane: {n['model_lane']}")
            if n.get("verifier_ref"):
                wf_lines.append(f"    verifier_ref: {n['verifier_ref']}")
            if n.get("gates"):
                gates_yaml = ", ".join(n["gates"])  # type: ignore[arg-type]
                wf_lines.append(f"    gates: [{gates_yaml}]")
    if edges:
        wf_lines.append("edges:")
        for e in edges:
            wf_lines.append(f"  - from: {e.get('from')}")
            wf_lines.append(f"    to: {e.get('to')}")
            et = e.get("edge_type")
            if et:
                wf_lines.append(f"    edge_type: {et}")
    (d / "workflow.yaml").write_text("\n".join(wf_lines) + "\n", encoding="utf-8")
    # artifact_contract.yaml
    if include_artifact and artifact is not None:
        ac_lines: list[str] = []
        if "expected_artifact" in artifact:
            ac_lines.append(f"expected_artifact: {artifact['expected_artifact']}")
        if "success_verifiers" in artifact:
            sv = artifact["success_verifiers"]
            assert isinstance(sv, list)
            ac_lines.append("success_verifiers:")
            for item in sv:
                ac_lines.append(f"  - {item}")
        if "failure_policy" in artifact:
            ac_lines.append(f"failure_policy: {artifact['failure_policy']}")
        if "rollback_policy" in artifact:
            ac_lines.append(f"rollback_policy: {artifact['rollback_policy']}")
        (d / "artifact_contract.yaml").write_text(
            "\n".join(ac_lines) + "\n", encoding="utf-8"
        )
    # prompts + verifiers
    if prompts:
        prompts_dir = d / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in prompts.items():
            (prompts_dir / fname).write_text(content, encoding="utf-8")
    if verifiers:
        verifiers_dir = d / "verifiers"
        verifiers_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in verifiers.items():
            (verifiers_dir / fname).write_text(content, encoding="utf-8")
    # example kickoff
    if example_kickoff:
        (d / "example-kickoff.md").write_text(example_kickoff, encoding="utf-8")
    return d


def _patch_engine(monkeypatch: pytest.MonkeyPatch, engine_root: Path) -> None:
    monkeypatch.setattr(
        "mini_ork.web.control._mini_ork_root", lambda: engine_root
    )


def _seed_task_run(
    home: Path,
    *,
    run_id: str,
    recipe: str,
    status: str,
    cost_usd: float = 0.0,
    age_seconds: int = 60,
) -> None:
    """Seed a ``task_runs`` row (kickoff_path omitted — the projection
    doesn't read it)."""
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """
        INSERT INTO task_runs
            (id, recipe, status, cost_usd, created_at, updated_at,
             task_class, kickoff_path, workflow_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            recipe,
            status,
            cost_usd,
            now - age_seconds,
            now - age_seconds,
            "framework_edit",
            str(home / "runs-inbox" / f"{run_id}.md"),
            "latest",
        ),
    )
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def _migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Migrate once per module; tests copy it."""
    db_path = tmp_path_factory.mktemp("template") / "state.db"
    rc, out, err = mig.init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return db_path


@pytest.fixture
def home(tmp_path: Path, _migrated_db: Path) -> Path:
    """A migrated ``.mini-ork`` home with seeded task_runs."""
    h = tmp_path / ".mini-ork"
    h.mkdir()
    (h / "runs-inbox").mkdir()
    shutil.copyfile(_migrated_db, h / "state.db")
    return h


# ── recipe_rows ─────────────────────────────────────────────────────────


def test_recipe_rows_lists_engine_and_project(monkeypatch, tmp_path):
    """``recipe_rows`` returns rows for both engine and project recipes."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "alpha", description="Alpha recipe", nodes=2)
    _write_recipe(engine / "recipes", "beta", description="Beta recipe", nodes=1)
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    _write_recipe(
        home / "recipes", "docs", description="Project docs", nodes=3
    )
    rows = recipe_view.recipe_rows(home)
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"alpha", "beta", "docs"}
    assert by_id["alpha"]["source"] == "engine"
    assert by_id["beta"]["source"] == "engine"
    assert by_id["docs"]["source"] == "project"


def test_recipe_rows_orders_project_first_then_runs_desc(monkeypatch, tmp_path, home):
    """Project entries come first; among ties, runs desc, then id asc."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "code-fix", description="c1", nodes=2)
    _write_recipe(engine / "recipes", "framework-edit", description="f1", nodes=1)
    _patch_engine(monkeypatch, engine)

    _write_recipe(home / "recipes", "docs", description="docs", nodes=1)
    _seed_task_run(home, run_id="r-docs-1", recipe="docs", status="published")
    _seed_task_run(home, run_id="r-docs-2", recipe="docs", status="published")
    _seed_task_run(home, run_id="r-cf-1", recipe="code-fix", status="published")
    _seed_task_run(home, run_id="r-cf-2", recipe="code-fix", status="rolled_back")

    rows = recipe_view.recipe_rows(home)
    # First rows are project; project entries with runs come before project
    # entries without runs (here we have only one project entry).
    assert rows[0]["id"] == "docs"
    # Engine entries follow, sorted by runs desc, then id asc.
    engine_rows = [r for r in rows if r["source"] == "engine"]
    assert [r["id"] for r in engine_rows] == ["code-fix", "framework-edit"]
    assert engine_rows[0]["runs"] == 2


def test_recipe_rows_marks_shadowing_project_entry(monkeypatch, tmp_path):
    """A project recipe that shadows an engine recipe renders as
    ``project (overrides engine)``."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "code-fix", description="e", nodes=2)
    _patch_engine(monkeypatch, engine)

    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    _write_recipe(home / "recipes", "code-fix", description="p", nodes=4)

    rows = recipe_view.recipe_rows(home)
    cf = next(r for r in rows if r["id"] == "code-fix")
    assert cf["source"] == "project (overrides engine)"
    assert cf["steps"] == 4


def test_recipe_rows_source_filter(monkeypatch, tmp_path):
    """``/recipes project`` and ``/recipes engine`` filter accordingly."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "alpha", description="e", nodes=1)
    _patch_engine(monkeypatch, engine)
    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    _write_recipe(home / "recipes", "docs", description="p", nodes=1)

    project = recipe_view.recipe_rows(home, source="project")
    assert [r["id"] for r in project] == ["docs"]
    engine_only = recipe_view.recipe_rows(home, source="engine")
    assert [r["id"] for r in engine_only] == ["alpha"]
    all_rows = recipe_view.recipe_rows(home, source="all")
    assert {r["id"] for r in all_rows} == {"alpha", "docs"}


def test_recipe_rows_text_filter(monkeypatch, tmp_path):
    """Free-text needle matches id, description, or keywords (case-insensitive)."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(
        engine / "recipes",
        "alpha",
        description="Quick fix",
        nodes=1,
    )
    _write_recipe(
        engine / "recipes",
        "beta",
        description="Long-form audit",
        keywords=("synthesis", "review"),
        nodes=1,
    )
    _patch_engine(monkeypatch, engine)
    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)

    needle_id = recipe_view.recipe_rows(home, text="alpha")
    assert [r["id"] for r in needle_id] == ["alpha"]
    needle_desc = recipe_view.recipe_rows(home, text="audit")
    assert [r["id"] for r in needle_desc] == ["beta"]
    needle_keyword = recipe_view.recipe_rows(home, text="synthesis")
    assert [r["id"] for r in needle_keyword] == ["beta"]
    miss = recipe_view.recipe_rows(home, text="nope")
    assert miss == []


def test_recipe_rows_handles_missing_artifact_contract(monkeypatch, tmp_path):
    """A recipe missing ``artifact_contract.yaml`` still scores; ``grade`` is
    ``—`` when ``eval_recipe`` raises or yields no findings (we accept
    either — the test only pins "doesn't crash")."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(
        engine / "recipes",
        "half",
        description="Half baked",
        nodes=2,
        include_artifact=False,
    )
    _patch_engine(monkeypatch, engine)
    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)

    rows = recipe_view.recipe_rows(home)
    assert len(rows) == 1
    assert rows[0]["id"] == "half"
    # The grade letter is either ``—`` (no eval result) or a letter from the
    # eval module — both are acceptable as long as the row materialised.
    assert rows[0]["grade_letter"] in {"—", "F", "D", "C", "B", "A"}


# ── render_recipes ──────────────────────────────────────────────────────


def test_render_recipes_includes_counts_and_table(monkeypatch, tmp_path):
    """The rendered markdown carries a header with source counts and a row
    per recipe, with the right column order."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "alpha", description="Alpha recipe", nodes=2)
    _write_recipe(engine / "recipes", "beta", description="Beta recipe", nodes=1)
    _patch_engine(monkeypatch, engine)
    home = tmp_path / "home" / ".mini-ork"
    home.mkdir(parents=True)
    _write_recipe(home / "recipes", "docs", description="My docs", nodes=3)

    rows = recipe_view.recipe_rows(home)
    md = recipe_view.render_recipes(rows, source="all")
    assert "Project 1 · Engine 2" in md
    # The table header + at least one row for each recipe.
    assert "| recipe | source | steps |" in md
    for rid in ("alpha", "beta", "docs"):
        assert f"| `{rid}` |" in md
    # Filter hint footer.
    assert "Filter: `/recipes project`" in md


def test_render_recipes_empty_returns_no_match():
    """No rows → ``No recipes match.``."""
    out = recipe_view.render_recipes([], source="all")
    assert out == "No recipes match."


# ── recipe_card ─────────────────────────────────────────────────────────


def test_recipe_card_includes_steps_flow_checks(monkeypatch, tmp_path, home):
    """A full card carries the steps table, a Flow line, the checks column,
    and the artifact contract rows."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(
        engine / "recipes",
        "docs",
        description="Document the system.",
        task_class_name="doc_class",
        keywords=("docs", "documentation", "manual"),
        nodes=[
            {
                "id": "planner",
                "type": "planner",
                "model_lane": "planner",
                "verifier_ref": "verifiers/static.py",
            },
            {
                "id": "doc_editor",
                "type": "implementer",
                "model_lane": "implementer",
                "verifier_ref": "verifiers/test.py",
                "gates": ("cost", "grade"),
            },
        ],
        edges=[
            {"from": "planner", "to": "doc_editor"},
            {"from": "doc_editor", "to": "grep_assert"},
            {"from": "doc_editor", "to": "link_verifier"},
            {"from": "doc_editor", "to": "publisher"},
        ],
        artifact={
            "expected_artifact": "framework-edit.diff",
            "success_verifiers": ["verifiers/test.py", "verifiers/static.py"],
            "failure_policy": "rollback",
            "rollback_policy": "git reset",
        },
        prompts={"planner.md": "# planner\n", "implementer.md": "# impl\n"},
        verifiers={"test.py": "pass\n", "static.py": "ok\n"},
        example_kickoff="# Doc this thing\n\nbody\n",
    )
    _patch_engine(monkeypatch, engine)

    card = recipe_view.recipe_card(home, "docs")
    assert card is not None
    assert card["id"] == "docs"
    assert card["task_class"] == "doc_class"
    assert card["keywords"][:3] == ["docs", "documentation", "manual"]
    assert len(card["nodes"]) == 2
    assert len(card["edges"]) == 4
    assert card["example_kickoff_heading"] == "Doc this thing"
    # Files include the YAML contracts, prompts, verifiers, example kickoff.
    file_names = [p.name for p in card["files"]]
    assert "workflow.yaml" in file_names
    assert "task_class.yaml" in file_names
    assert "artifact_contract.yaml" in file_names
    assert "planner.md" in file_names
    assert "implementer.md" in file_names
    assert "test.py" in file_names
    assert "static.py" in file_names
    assert "example-kickoff.md" in file_names
    # Grade is a (letter, score) pair.
    assert card["grade"]["letter"] in {"A", "B", "C", "D", "F"}
    assert isinstance(card["grade"]["score"], int)

    md, files = recipe_view.render_recipe_card(card)
    # Steps table.
    assert "| step | type | model (role → lane) | checks |" in md
    assert "| `planner` |" in md
    assert "| `doc_editor` |" in md
    # Flow line: planner → doc_editor → grep_assert, link_verifier, publisher.
    assert "Flow:" in md
    assert "planner → doc_editor" in md
    assert "grep_assert" in md
    # Checks column lists verifier filenames + gates.
    assert "test.py" in md
    assert "gate:cost" in md
    # Artifact contract rows.
    assert "framework-edit.diff" in md
    assert "rollback" in md
    # Track record line + the "Run it" line.
    assert "Run it: `/run" in md
    assert files == card["files"]


def test_recipe_card_unknown_id_returns_none(monkeypatch, tmp_path, home):
    """An unknown recipe id resolves to ``None`` so the handler can render
    a one-line "No recipe <id>" reply."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "alpha", description="a", nodes=1)
    _patch_engine(monkeypatch, engine)
    assert recipe_view.recipe_card(home, "does-not-exist") is None


def test_recipe_card_track_record_numbers(monkeypatch, tmp_path, home):
    """The card's ``track_record`` reads runs / published / success % /
    avg cost from the seeded ``task_runs`` rows."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "docs", description="d", nodes=1)
    _patch_engine(monkeypatch, engine)
    _seed_task_run(home, run_id="r1", recipe="docs", status="published", cost_usd=0.10)
    _seed_task_run(home, run_id="r2", recipe="docs", status="published", cost_usd=0.20)
    _seed_task_run(home, run_id="r3", recipe="docs", status="rolled_back", cost_usd=0.30)
    _seed_task_run(home, run_id="r4", recipe="docs", status="failed", cost_usd=0.40)

    card = recipe_view.recipe_card(home, "docs")
    assert card is not None
    tr = card["track_record"]
    assert tr["runs"] == 4
    # 4 finished (published + rolled_back + failed), 2 published → 50%.
    assert tr["finished"] == 4
    assert tr["published"] == 2
    assert tr["success_pct"] == pytest.approx(50.0)
    # avg_cost_usd is the average over FINISHED runs (matches the kickoff).
    assert tr["avg_cost_usd"] == pytest.approx((0.10 + 0.20 + 0.30 + 0.40) / 4)

    md, files = recipe_view.render_recipe_card(card)
    assert "Track record: 4 runs · 50% success" in md
    # Files list is the second tuple element — sanity-check shape.
    assert isinstance(files, list)
    assert all(isinstance(f, Path) for f in files)


def test_recipe_card_recent_runs_limited_to_5(monkeypatch, tmp_path, home):
    """``recent_runs`` is the most-recent 5 (newest-first)."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "docs", description="d", nodes=1)
    _patch_engine(monkeypatch, engine)
    # Seed 8 runs — 5 newest should surface. Smaller ``age_seconds`` is
    # closer to "now", so r-00 is the newest row.
    for i in range(8):
        _seed_task_run(
            home,
            run_id=f"r-{i:02d}",
            recipe="docs",
            status="published",
            age_seconds=10 + i * 10,
        )
    card = recipe_view.recipe_card(home, "docs")
    assert card is not None
    assert len(card["recent_runs"]) == 5
    # Newest first: r-00 (smallest age = closest to now), r-04 last.
    assert card["recent_runs"][0]["id"] == "r-00"
    assert card["recent_runs"][-1]["id"] == "r-04"


# ── describe_recipe_payload (MCP-friendly) ──────────────────────────────


def test_describe_recipe_payload_returns_paths_as_strings(
    monkeypatch, tmp_path, home
):
    """The MCP-facing variant serialises Path fields to str."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(
        engine / "recipes",
        "docs",
        description="d",
        nodes=[{"id": "n1", "type": "verifier"}],
    )
    _patch_engine(monkeypatch, engine)

    payload = recipe_view.describe_recipe_payload(home, "docs")
    assert payload is not None
    assert isinstance(payload["files"], list)
    for item in payload["files"]:
        assert isinstance(item, str)
    assert isinstance(payload["path"], str)


def test_describe_recipe_payload_unknown_id_returns_none(monkeypatch, tmp_path, home):
    """``None`` on miss — the MCP tool wraps it in ``{"error": ...}``."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _patch_engine(monkeypatch, engine)
    assert recipe_view.describe_recipe_payload(home, "nope") is None


def test_recipe_view_import_does_not_crash_even_with_no_db(
    monkeypatch, tmp_path
):
    """A home with no db (or no engine) still yields rows + cards without
    raising — the projection must always be safe to call."""
    engine = tmp_path / "engine"
    engine.mkdir()
    _write_recipe(engine / "recipes", "alpha", description="a", nodes=1)
    _patch_engine(monkeypatch, engine)
    bare = tmp_path / "home" / ".mini-ork"
    bare.mkdir(parents=True)
    # No ``state.db`` under this home: ``recipe_rows`` and ``recipe_card``
    # must still return coherent output.
    rows = recipe_view.recipe_rows(bare)
    assert rows[0]["id"] == "alpha"
    card = recipe_view.recipe_card(bare, "alpha")
    assert card is not None
    assert card["track_record"]["runs"] == 0