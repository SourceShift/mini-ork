"""``mini-ork board page inbox`` / ``kanban`` — the attention-first inbox and the board.

The fixture pattern mirrors ``test_ide_pages_run.py``: a temp mini-ork home with
an initialised DB, a ``task_runs`` row per run, a run dir, and a kickoff file
(the page titles come from the kickoff's first line).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import spec as S
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed_run(home: Path, run_id: str, *, title: str, status: str, created: int,
              cost: float = 0.0, recipe: str = "demo-recipe") -> Path:
    """One ``task_runs`` row + run dir + kickoff. ``created`` is epoch seconds.

    ``recipe=""`` keeps a row cheap to render: the page's per-row loader and
    the retry-hint scan resolve a recipe through the whole catalog, and a
    cap-test row only needs to exist, not resolve a workflow.
    """
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / f"{run_id}.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text(f"# {title}\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, cost, created, created + 30, created + 30, "demo",
         str(kickoff), "latest", f"tr-{run_id}"))
    con.commit()
    con.close()
    return run_dir


def _needs_you(home: Path, run_id: str, title: str, created: int) -> Path:
    """A run the fleet reads as ``needs_you`` — a cost-pause sentinel on a live run."""
    run_dir = _seed_run(home, run_id, title=title, status="executing", created=created)
    (run_dir / ".cost-pause").write_text("")
    return run_dir


def _failed(home: Path, run_id: str, title: str, created: int, *, cost: float = 0.0,
            recipe: str = "demo-recipe") -> Path:
    return _seed_run(home, run_id, title=title, status="failed", created=created, cost=cost,
                     recipe=recipe)


def _section(page: dict[str, Any], kind: str, title: str | None = None) -> dict[str, Any]:
    for s in page["sections"]:
        if s["type"] == kind and (title is None or s["title"] == title):
            return s
    raise AssertionError(f"no {kind} section {title!r} in "
                         f"{[s['title'] for s in page['sections']]}")


# ── inbox ──────────────────────────────────────────────────────────────────

def test_inbox_orders_needs_you_then_failed_and_hides_landed_and_repairing(home: Path) -> None:
    now = int(time.time())
    _needs_you(home, "run-needs", "Needs me now", now - 10)
    _failed(home, "run-old", "Failed older", now - 300)
    _failed(home, "run-new", "Failed newer", now - 100)
    landed_dir = _failed(home, "run-landed", "Landed elsewhere", now - 50)
    (landed_dir / "landed.json").write_text(json.dumps(
        {"commit": "abc1234567890", "repo": str(home), "note": "delivered by hand"}))
    repairing_dir = _failed(home, "run-repair", "Being repaired now", now - 20)
    (repairing_dir / "repair.json").write_text(json.dumps({"state": "repairing"}))

    page = build_page(home, "inbox")
    assert page["ok"] is True and page["key"] == "inbox" and page["label"] == "Inbox"

    # needs_you first, then failed; newest first inside each.
    titles = [s["title"] for s in page["sections"] if s["type"] == "callout"]
    assert titles == ["Needs me now", "Failed newer", "Failed older"]

    repaired = _section(page, "list", "Being repaired")
    assert [i["t"] for i in repaired["items"]] == ["Being repaired now"]
    # The landed run is done via landed.json — gone from the inbox entirely.
    assert "Landed elsewhere" not in json.dumps(page)


def test_inbox_row_is_a_callout_with_the_outcome_and_an_open_run_button(home: Path) -> None:
    now = int(time.time())
    _needs_you(home, "run-needs", "Needs me now", now - 10)
    page = build_page(home, "inbox")
    row = _section(page, "callout", "Needs me now")
    assert row["full"] is True and row["tone"] == "yellow"
    labels = [a["label"] for a in row["actions"]]
    assert labels[-1] == "Open run"
    assert row["actions"][-1]["do"] == {"run": "run-needs", "title": "Needs me now"}

    chips = {c["t"]: c["c"] for c in page["chips"]}
    assert chips == {"✋ needs you 1": "yellow", "✗ failed 0": "red"}


def test_chips_report_failed_and_being_repaired_counts(home: Path) -> None:
    now = int(time.time())
    _needs_you(home, "run-needs", "Needs me", now - 10)
    _failed(home, "run-fail", "It failed", now - 20)
    repairing_dir = _failed(home, "run-repair", "Being repaired", now - 5)
    (repairing_dir / "repair.json").write_text(json.dumps({"state": "repairing"}))
    page = build_page(home, "inbox")
    assert [c["t"] for c in page["chips"]] == [
        "✋ needs you 1", "✗ failed 1", "⟳ being repaired 1"]


def test_inbox_caps_rows_and_links_the_runs_page(home: Path) -> None:
    now = int(time.time())
    for i in range(30):
        _failed(home, f"run-{i:02d}", f"Failed {i:02d}", now - 1000 + i, recipe="")
    page = build_page(home, "inbox")
    # The newest rows get a full outcome card; the rest of the 25 are compact
    # rows, so the page stays fast on a large home.
    callouts = [s for s in page["sections"] if s["type"] == "callout"]
    assert len(callouts) == 8
    assert len(_section(page, "list", "Also waiting")["items"]) == 25 - 8
    more = _section(page, "list", "More")
    assert more["items"][0]["t"] == "5 more…"
    assert more["items"][0]["acts"][0]["do"] == {"page": "runs"}


def test_empty_inbox_says_nothing_needs_you(home: Path) -> None:
    page = build_page(home, "inbox")
    assert page["ok"] is True
    callouts = [s for s in page["sections"] if s["type"] == "callout"]
    assert len(callouts) == 1
    assert callouts[0]["title"] == "Nothing needs you"
    assert callouts[0]["tone"] == "green"
    assert [c["t"] for c in page["chips"]] == ["✋ needs you 0", "✗ failed 0"]


def test_a_broken_row_costs_its_row_only(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = int(time.time())
    _failed(home, "run-a", "First failed", now - 20)
    _failed(home, "run-b", "Second failed", now - 10)
    from mini_ork.ide_pages import inbox as inbox_page

    good = inbox_page._row

    def boom(home_: Path, run_id: str):
        if run_id == "run-b":
            raise RuntimeError("disk gone")
        return good(home_, run_id)

    monkeypatch.setattr(inbox_page, "_row", boom)
    page = build_page(home, "inbox")
    assert page["ok"] is True
    assert [s["title"] for s in page["sections"] if s["type"] == "callout"] == ["First failed"]
    broken = _section(page, "list", "run-b")
    assert broken["items"][0]["m"] == "✗" and "disk gone" in broken["items"][0]["sub"]


# ── kanban ─────────────────────────────────────────────────────────────────

def test_kanban_groups_every_state_into_columns(home: Path) -> None:
    now = int(time.time())
    _seed_run(home, "run-work", title="Working hard", status="executing", created=now - 60,
              cost=0.25)
    _needs_you(home, "run-needs", "Needs me", now - 30)
    _failed(home, "run-fail", "It failed", now - 20, cost=0.50)
    _seed_run(home, "run-done", title="All done", status="published", created=now - 10)
    landed_dir = _failed(home, "run-landed", "Landed here", now - 5)
    (landed_dir / "landed.json").write_text(json.dumps(
        {"commit": "deadbeef123", "repo": str(home), "note": ""}))

    page = build_page(home, "kanban")
    assert page["ok"] is True and page["key"] == "kanban" and page["label"] == "Board"

    sec = _section(page, "columns")
    assert sec["full"] is True
    assert [c["title"] for c in sec["cols"]] == ["Working", "Needs you", "Failed", "Done"]
    assert [c["count"] for c in sec["cols"]] == [1, 1, 1, 2]
    by_title = {c["title"]: c for c in sec["cols"]}
    assert by_title["Working"]["cards"][0]["title"] == "Working hard"
    assert by_title["Needs you"]["cards"][0]["title"] == "Needs me"
    assert by_title["Failed"]["cards"][0]["title"] == "It failed"
    # A landed run is done, so it lands under Done alongside the published run.
    assert sorted(c["title"] for c in by_title["Done"]["cards"]) == ["All done", "Landed here"]

    states = {"Working": "working", "Needs you": "needs_you",
              "Failed": "failed", "Done": "done"}
    for col in sec["cols"]:
        for card in col["cards"]:
            # Every card carries the documented keys and opens its run.
            assert set(card) == {"id", "title", "sub", "state", "mark", "meta", "do"}
            assert card["do"] == {"run": card["id"], "title": card["title"]}
            assert card["state"] == states[col["title"]]
    work = by_title["Working"]["cards"][0]
    assert work["sub"] == "demo-recipe"
    assert work["meta"][0] == {"t": "$0.25", "c": "sub", "mono": True}


def test_kanban_done_covers_only_the_last_24h(home: Path) -> None:
    now = int(time.time())
    _seed_run(home, "run-today", title="Today", status="published", created=now - 3600)
    _seed_run(home, "run-old", title="Last week", status="published", created=now - 8 * 86400)
    page = build_page(home, "kanban")
    done = next(c for c in _section(page, "columns")["cols"] if c["title"] == "Done")
    assert [c["title"] for c in done["cards"]] == ["Today"]


def test_kanban_caps_each_column_at_30_newest_first(home: Path) -> None:
    now = int(time.time())
    for i in range(35):
        _failed(home, f"run-{i:02d}", f"Failed {i:02d}", now - 1000 + i)
    page = build_page(home, "kanban")
    failed = next(c for c in _section(page, "columns")["cols"] if c["title"] == "Failed")
    assert failed["count"] == 30
    assert [c["title"] for c in failed["cards"]][:3] == ["Failed 34", "Failed 33", "Failed 32"]


# ── spec helpers ───────────────────────────────────────────────────────────

def test_spec_composer_and_columns_produce_the_documented_keys() -> None:
    comp = S.composer("Steer the run…", ["board", "steer", "run-x", "--text"],
                      title="Steer")
    assert comp["type"] == "composer"
    assert comp["title"] == "Steer"
    assert comp["placeholder"] == "Steer the run…"
    assert comp["cli"] == ["board", "steer", "run-x", "--text"]

    card = {"id": "run-x", "title": "T", "sub": "demo · step", "state": "working",
            "mark": "●", "meta": [S.meta_item("$0.10", mono=True)],
            "do": S.open_run("run-x", "T")}
    section = S.columns("Board", [S.column("Working", [card], c="blue")])
    assert section["type"] == "columns"
    col = section["cols"][0]
    assert set(col) == {"title", "c", "count", "cards"}
    assert col["title"] == "Working" and col["c"] == "blue" and col["count"] == 1
    assert col["cards"][0]["do"] == {"run": "run-x", "title": "T"}


# ── both pages build ───────────────────────────────────────────────────────

def test_build_page_returns_ok_for_both_pages(home: Path) -> None:
    for key, label in (("inbox", "Inbox"), ("kanban", "Board")):
        page = build_page(home, key)
        assert page["ok"] is True and page["key"] == key and page["label"] == label
        json.dumps(page)  # the payload stays JSON-serialisable
