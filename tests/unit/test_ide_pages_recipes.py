"""IDE page ``recipes`` — catalog + detail, specs, epics."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import recipes
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _section(page: dict, title: str) -> dict:
    return next(s for s in page["sections"] if s["title"] == title)


def _sql(home: Path, *statements: tuple[str, tuple]) -> None:
    con = sqlite3.connect(home / "state.db")
    for sql, params in statements:
        con.execute(sql, params)
    con.commit()
    con.close()


def test_tabs_title_and_header_match_the_design(home: Path) -> None:
    for key, _label in recipes.TABS:
        page = build_page(home, "recipes", key, {})
        assert page["ok"] is True and page["errors"] == {}, page
        assert page["title"] == "Recipes, specs & epics"
        assert [(t["key"], t["label"]) for t in page["tabs"]] == [
            ("recipes", "Recipes"), ("specs", "Specs"), ("epics", "Epics & roadmap")]
        assert page["chips"][0]["t"].endswith(" recipes")
        assert page["actions"] == [{"label": "New recipe", "kind": "primary", "do": {"thread": "/recipe new "}}]
        json.dumps(page)


def test_catalog_is_a_list_detail_with_the_selected_recipe(home: Path) -> None:
    _sql(home, ("INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, task_class, "
                "kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
                ("run-1", "code-fix", "published", 0.5, int(time.time()), int(time.time()),
                 "code_fix", "", "latest")))
    page = build_page(home, "recipes", "recipes", {"recipe": "code-fix"})
    catalog = _section(page, "Catalog")
    assert catalog["type"] == "table" and catalog["col"] == 1
    assert catalog["head"] == ["recipe", "source", "grade", "runs", "ok", "avg"]
    row = next(r for r in catalog["rows"] if r["cells"][0]["t"] == "code-fix")
    assert row["sel"] is True and row["do"] == {"set": {"recipe": "code-fix"}}
    assert [c["t"] for c in row["cells"][3:]] == ["1", "100%", "$0.50"]
    detail = _section(page, "code-fix")
    assert detail["col"] == 2
    assert {i["k"] for i in detail["items"]} == {"Grade", "Runs", "Success", "Avg cost"}
    assert detail["actions"][0]["do"] == {"thread": "/run code-fix "}
    flow = [i["label"] for i in _section(page, "Flow")["items"]]
    assert flow[:2] == ["planner", "implementer"] and "reviewer" in flow
    contract = [i["t"] for i in _section(page, "Contract")["items"]]
    assert contract[0] == "Produces: patch"
    assert "verifier test.py" in contract
    assert _section(page, "Checks")["items"][0]["t"].startswith("recipe-eval")


def test_an_unknown_recipe_arg_falls_back_to_the_first_row(home: Path) -> None:
    page = build_page(home, "recipes", "recipes", {"recipe": "no-such-recipe"})
    first = _section(page, "Catalog")["rows"][0]["cells"][0]["t"]
    assert page["args"]["recipe"] == first
    assert _section(page, first)


def test_specs_read_a_spec_index_or_say_how_to_make_one(home: Path) -> None:
    page = build_page(home, "recipes", "specs", {})
    assert "No specs ingested" in page["sections"][0]["rows"][0]["cells"][0]["t"]
    specs = home.parent / "specs"
    specs.mkdir()
    (specs / "spec-index.json").write_text(json.dumps({
        "schema_version": 1, "root": str(specs),
        "specs": {"durable-dag": {"source_path": "durable-dag.spec.md", "title": "Durable DAG",
                                  "status": "ready", "depends_on": ["remote-nodes"]}}}))
    page = build_page(home, "recipes", "specs", {})
    row = page["sections"][0]["rows"][0]
    assert [c["t"] for c in row["cells"]] == ["durable-dag", "Durable DAG", "ready", "1"]
    assert row["do"]["path"].endswith("durable-dag.spec.md")


def test_epics_table_dependencies_and_attention(home: Path) -> None:
    _sql(home,
         ("INSERT INTO epics (id, title, status, lane) VALUES (?,?,?,?)", ("e1", "Env profiles", "in progress", "glm")),
         ("INSERT INTO epics (id, title, status) VALUES (?,?,?)", ("e2", "Secrets and doctor", "blocked")),
         ("INSERT INTO epics (id, title, status) VALUES (?,?,?)", ("e3", "Sandbox reaper", "escalated")),
         ("INSERT INTO epic_dependencies (from_epic_id, to_epic_id) VALUES (?,?)", ("e1", "e2")))
    page = build_page(home, "recipes", "epics", {})
    table = _section(page, "Epics")
    assert table["head"] == ["epic", "status", "recipe", "prio", "attempts"]
    assert [r["cells"][0]["t"] for r in table["rows"]] == ["Env profiles", "Sandbox reaper", "Secrets and doctor"]
    assert table["rows"][0]["cells"][1]["c"] == "blue"
    assert _section(page, "Dependencies")["lines"][0]["t"] == "e1 ──▶ e2"
    attention = _section(page, "Needs attention")["items"]
    assert attention[0]["m"] == "✗" and attention[0]["t"] == "e3 escalated"


def test_board_page_cli_returns_the_page(home: Path, capsys) -> None:
    assert board_cmd.main(["page", "recipes", "--tab", "epics", "--home", str(home)], "") == 0
    page = json.loads(capsys.readouterr().out)
    assert page["ok"] is True and page["key"] == "recipes" and page["tab"] == "epics"
