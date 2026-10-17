"""The run page's level-2 Artifacts tab: rows grouped by node and kind.

``_artifact_kind`` is the single extension rule behind a row's ``kind`` key,
the intra-table order (patch → json → log → jsonl → md → other) and the
per-table count sub. ``spec.table`` rebuilds every row and keeps only
cells/do/sel/acts, so every assertion here goes through ``build_page``: a
``kind`` that does not survive into the rendered page is not met.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import run as run_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
SIX = {"patch", "json", "log", "jsonl", "md", "other"}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, run_id: str, *, files: tuple[str, ...] = (),
          nodes: tuple[tuple[str, str], ...] = ()) -> str:
    """A run dir holding ``files`` plus its ``task_runs`` row and node events."""
    run_dir = home / "runs" / run_id
    for rel in files:
        target = run_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x" * 8, encoding="utf-8")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at,"
        " updated_at, ended_at, task_class, kickoff_path, trace_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", "executing", 0.0, 1_791_000_000,
         1_791_000_002, None, "framework_edit", "", "tr-artifacts"))
    for node_id, node_type in nodes:
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json,"
            " created_at) VALUES (?,?,?,?,?)",
            (f"ev-{node_id}", run_id, "node_start",
             json.dumps({"node_id": node_id, "node_type": node_type}),
             1_791_000_001))
    con.commit()
    con.close()
    return run_id


def _page(monkeypatch: pytest.MonkeyPatch, home: Path, run_id: str) -> dict:
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, "run", "artifacts", {"run": run_id})
    assert page.get("ok") is True, f"artifacts tab failed: {page.get('error')}"
    return page


def _artifact_tables(page: dict) -> list[dict]:
    return [s for s in page["sections"]
            if s.get("title") == "Key artifacts"
            or str(s.get("title") or "").startswith("From ")
            or s.get("title") == "Run files"]


def _table(page: dict, title: str) -> dict:
    matches = [s for s in page["sections"] if s.get("title") == title]
    assert matches, (f"no section {title!r}; sections: "
                     f"{[s.get('title') for s in page['sections']]}")
    return matches[0]


def _row_text(row: dict) -> str:
    return str((row.get("cells") or [{}])[0].get("t", ""))


def _file_rows(section: dict) -> list[dict]:
    return [r for r in (section.get("rows") or [])
            if not _row_text(r).startswith("+")
            and not _row_text(r).startswith("No ")]


def test_node_bucket_orders_rows_by_kind(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One node's table reads patch → json → log → md, each row carrying its kind."""
    run_id = _seed(home, "run-node-order",
                   files=("impl-writer.md", "impl-writer.log", "impl-writer.patch",
                          "impl-writer.json", "agent-writer.json"),
                   nodes=(("writer", "implementer"),))
    page = _page(monkeypatch, home, run_id)

    rows = _file_rows(_table(page, "From writer"))
    assert [r.get("kind") for r in rows] == ["patch", "json", "json", "log", "md"]
    # Ties keep the flat listing's path order (agent-… before impl-…).
    assert [_row_text(r) for r in rows] == [
        "impl-writer.patch", "agent-writer.json", "impl-writer.json",
        "impl-writer.log", "impl-writer.md"]


def test_node_table_sub_names_each_kind_with_its_count(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sub lists ``<n> <kind>`` in canonical order, derived from the rows."""
    run_id = _seed(home, "run-node-sub",
                   files=("impl-writer.patch", "impl-writer.json",
                          "agent-writer.json", "impl-writer.log", "impl-writer.md"),
                   nodes=(("writer", "implementer"),))
    page = _page(monkeypatch, home, run_id)

    section = _table(page, "From writer")
    assert section.get("sub") == "1 patch · 2 json · 1 log · 1 md"


def test_single_file_bucket_has_no_sub(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-file table has nothing to summarise, so it shows no sub."""
    run_id = _seed(home, "run-solo", files=("impl-solo.md",),
                   nodes=(("solo", "implementer"),))
    page = _page(monkeypatch, home, run_id)

    section = _table(page, "From solo")
    rows = _file_rows(section)
    assert [(_row_text(r), r.get("kind")) for r in rows] == [("impl-solo.md", "md")]
    assert not section.get("sub")


def test_run_files_bucket_orders_all_six_kinds(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run-level bucket orders and sub-labels by the same six-kind rule."""
    run_id = _seed(home, "run-runfiles",
                   files=("review-diff.patch", "run.json", "execute.log",
                          "trace.jsonl", "notes.md", "Makefile"))
    page = _page(monkeypatch, home, run_id)

    section = _table(page, "Run files")
    rows = _file_rows(section)
    assert [r.get("kind") for r in rows] == ["patch", "json", "log", "jsonl", "md", "other"]
    assert [_row_text(r) for r in rows] == [
        "review-diff.patch", "run.json", "execute.log", "trace.jsonl",
        "notes.md", "Makefile"]
    assert section.get("sub") == "1 patch · 1 json · 1 log · 1 jsonl · 1 md · 1 other"


def test_row_cap_holds_and_one_more_row_accounts_for_leftovers(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap still counts file rows across tables and one row accounts for
    everything left out."""
    run_id = _seed(home, "run-cap",
                   files=("verdict.json", "rubric.json", "execute.log",
                          "impl-capn.json", "impl-capn.log", "impl-capn.md",
                          "agent-capn.md", "lens-capn.md", "extra.md"),
                   nodes=(("capn", "implementer"),))
    monkeypatch.setattr(run_page, "_ARTIFACT_ROW_CAP", 6)
    page = _page(monkeypatch, home, run_id)

    assert len(_file_rows(_table(page, "Key artifacts"))) == 3
    file_tables = [s for s in _artifact_tables(page)
                   if s.get("title") != "Key artifacts"]
    assert sum(len(_file_rows(s)) for s in file_tables) == 3
    more = [r for s in file_tables for r in (s.get("rows") or [])
            if _row_text(r).startswith("+")]
    assert len(more) == 1, [ _row_text(r) for r in more ]
    assert _row_text(more[0]).startswith("+6 more")
    assert (file_tables[-1].get("rows") or [])[-1] is more[0]


def test_key_artifacts_first_and_its_rows_carry_kind(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Key artifacts stays the first artifacts section, unrestyled, with a kind
    on each file row."""
    run_id = _seed(home, "run-key",
                   files=("framework-edit.diff", "verdict.json",
                          "evidence/trace.log", "execute.log", "impl-imp.md"),
                   nodes=(("imp", "implementer"),))
    page = _page(monkeypatch, home, run_id)

    titles = [s.get("title") for s in _artifact_tables(page)]
    assert titles[0] == "Key artifacts"
    assert titles[1:] == ["From imp", "Run files"]
    key = _table(page, "Key artifacts")
    assert key.get("head") == ["file", "kind", "size", "from"]
    kinds = {_row_text(r): r.get("kind") for r in _file_rows(key)}
    assert kinds == {"framework-edit.diff": "patch", "verdict.json": "json",
                     "evidence/": "other", "execute.log": "log"}


def test_every_emitted_file_row_kind_is_one_of_six(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every emitted file row, in every artifacts table, carries a kind from the
    six-value vocabulary."""
    run_id = _seed(home, "run-every-kind",
                   files=("impl-writer.patch", "impl-writer.json", "impl-writer.log",
                          "impl-writer.md", "impl-writer.jsonl", "changes.diff",
                          "run.json", "trace.jsonl", "execute.log", "notes.md",
                          "Makefile", "verdict.json", "framework-edit.diff",
                          "evidence/trace.log"),
                   nodes=(("writer", "implementer"),))
    page = _page(monkeypatch, home, run_id)

    checked = 0
    for section in _artifact_tables(page):
        assert section.get("head") == ["file", "kind", "size", "from"]
        for row in _file_rows(section):
            assert row.get("kind") in SIX, (
                f"{_row_text(row)!r} in {section.get('title')!r} has "
                f"kind={row.get('kind')!r}")
            assert len(row.get("cells") or []) == 4
            checked += 1
    assert checked >= 14, checked
