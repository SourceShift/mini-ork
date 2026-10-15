"""``mini-ork board page kanban`` — the two-tab Board (epics and runs).

Acceptance matrix for the Tasks tab (default):

1. Default tab is ``tasks``; a ``New task`` thread action rides on top.
2. Every documented status lands in the right place — ``not started`` →
   Backlog; ``in progress`` → Working; ``in review`` / ``blocked`` with a
   held_reason / ``escalated`` / unknown → Needs you; ``blocked`` without a
   held_reason → Backlog; ``done`` → Done.
3. Every card's ``do`` opens the epic's own page (``page 'task'``, keyed by
   uid). The latest attempt's run and the kickoff file used to be the card's
   ``do``; they now live on that page.
4. Archived epics are hidden.
5. Each column caps at 30; sort is priority DESC then updated_at DESC.
6. A home without an ``epics`` table still renders — one muted line in the
   Tasks section, the page keeps ``ok: true``.

The Runs tab is the previous behaviour: ``tab='runs'`` returns exactly
today's four-column run board.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import build_page
from mini_ork import scheduler
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real mini-ork home with the migrations run.

    The scheduler migrations are idempotent and create ``epics.priority``,
    ``epics.held_reason``, ``epics.attempts``, and ``epic_attempts`` — the
    page tolerates their absence via PRAGMA table_info, but the acceptance
    tests exercise them on a fully-migrated home so the success path is
    covered too.
    """
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    scheduler.ensure_priority_column(str(h / "state.db"))
    scheduler.ensure_retry_schema(str(h / "state.db"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "proj"))
    return h


def _seed_epic(home: Path, epic_id: str, *, title: str | None = None,
               status: str = "not started", priority: int = 0,
               kickoff_path: str | None = None, held_reason: str | None = None,
               archived_at: str | None = None, attempts: int = 0,
               recipe: str | None = None) -> None:
    """Insert one ``epics`` row + apply held_reason / archived_at after the fact.

    The DDL declares the base columns; ``held_reason`` / ``attempts`` /
    ``recipe`` come from ``scheduler.ensure_retry_schema``. ``archived_at``
    comes from the original DDL.
    """
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO epics(id, title, status, kickoff_path, priority, "
        "created_at, updated_at) VALUES (?,?,?,?,?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (epic_id, title or epic_id, status, kickoff_path, priority),
    )
    if held_reason is not None:
        con.execute("UPDATE epics SET held_reason=? WHERE id=?", (held_reason, epic_id))
    if archived_at is not None:
        con.execute("UPDATE epics SET archived_at=? WHERE id=?", (archived_at, epic_id))
    if attempts:
        con.execute("UPDATE epics SET attempts=? WHERE id=?", (attempts, epic_id))
    if recipe is not None:
        con.execute("UPDATE epics SET recipe=? WHERE id=?", (recipe, epic_id))
    con.commit(); con.close()


def _seed_epic_attempt(home: Path, epic_id: str, *, run_id: str | None,
                       attempt: int, kickoff_path: str | None = None,
                       verdict: str | None = None) -> None:
    """One ``epic_attempts`` row — the join gives the card its ``do``."""
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO epic_attempts(epic_id, attempt, run_id, kickoff_path, "
        "verdict, started_at) VALUES (?,?,?,?,?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (epic_id, attempt, run_id, kickoff_path, verdict),
    )
    con.commit(); con.close()


def _seed_run(home: Path, run_id: str, *, title: str, status: str, created: int,
              cost: float = 0.0, recipe: str = "demo-recipe") -> Path:
    """A ``task_runs`` row + run dir + kickoff so the Runs tab builds."""
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
    con.commit(); con.close()
    return run_dir


def _section(page: dict[str, Any], kind: str, title: str | None = None) -> dict[str, Any]:
    for s in page["sections"]:
        if s["type"] == kind and (title is None or s["title"] == title):
            return s
    raise AssertionError(f"no {kind} section {title!r} in "
                         f"{[s['title'] for s in page['sections']]}")


def _cols(page: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The Board → title map of cards in the columns section."""
    sec = _section(page, "columns")
    return {c["title"]: list(c["cards"]) for c in sec["cols"]}


# ── default tab + header thread action ──────────────────────────────────────


def test_default_tab_is_tasks_with_new_task_thread_action(home: Path) -> None:
    """No tab argument → ``tasks`` is the default; a New task thread action
    rides on the page header."""
    page = build_page(home, "kanban")
    assert page["ok"] is True
    assert page["tab"] == "tasks"
    tabs = [(t["key"], t["label"]) for t in page["tabs"]]
    # ``sources`` is the PM Sources pane — the connect-from-the-UI surface
    # (declared sources, an explicit Test, and the Add boxes). It rides the
    # strip without moving the default tab off ``tasks``.
    assert tabs == [("tasks", "Tasks"), ("runs", "Runs"), ("sources", "Sources")]
    new_task = next((a for a in page["actions"] if a["label"] == "New task"), None)
    assert new_task is not None, page["actions"]
    assert new_task["kind"] == "primary"
    # Thread action, not cli — test_ide_pages_actions walks every cli
    # action the IDE exposes, so a cli action would have to parse.
    assert new_task["do"] == {"thread": "/task "}


# ── status → column mapping ────────────────────────────────────────────────


def test_not_started_lands_in_backlog(home: Path) -> None:
    _seed_epic(home, "e-backlog", title="Backlog epic", status="not started")
    page = build_page(home, "kanban")
    cols = _cols(page)
    assert [c["id"] for c in cols["Backlog"]] == ["e-backlog"]
    assert cols["Working"] == [] and cols["Done"] == []
    assert cols["Needs you"] == []


def test_in_progress_lands_in_working(home: Path) -> None:
    _seed_epic(home, "e-working", title="Working epic", status="in progress")
    page = build_page(home, "kanban")
    cols = _cols(page)
    assert [c["id"] for c in cols["Working"]] == ["e-working"]
    assert cols["Backlog"] == [] and cols["Done"] == [] and cols["Needs you"] == []


def test_in_review_lands_in_needs_you(home: Path) -> None:
    """'in review' is outside the kickoff's mapping table — never silently dropped."""
    _seed_epic(home, "e-review", title="Under review", status="in review")
    page = build_page(home, "kanban")
    cols = _cols(page)
    assert [c["id"] for c in cols["Needs you"]] == ["e-review"]
    assert cols["Backlog"] == [] and cols["Working"] == [] and cols["Done"] == []


def test_held_blocked_lands_in_needs_you(home: Path) -> None:
    """A scheduler hold for a human goes to Needs you."""
    _seed_epic(home, "e-held", title="Held for me", status="blocked",
               held_reason="awaiting reviewer approval")
    page = build_page(home, "kanban")
    cols = _cols(page)
    assert [c["id"] for c in cols["Needs you"]] == ["e-held"]
    held_card = cols["Needs you"][0]
    assert "held: awaiting reviewer approval" in held_card["sub"]


def test_unheld_blocked_lands_in_backlog(home: Path) -> None:
    """A blocked epic the system is holding for a dep (no held_reason) is Backlog."""
    _seed_epic(home, "e-blocked", title="Waiting on deps", status="blocked")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO epic_dependencies(from_epic_id, to_epic_id, kind) VALUES (?, ?, 'hard')",
        ("e-other", "e-blocked"),
    )
    con.commit(); con.close()
    page = build_page(home, "kanban")
    cols = _cols(page)
    assert [c["id"] for c in cols["Backlog"]] == ["e-blocked"]


def test_dependency_blocked_not_started_lands_in_backlog(home: Path) -> None:
    """A 'not started' epic with an open dependency edge into it is Backlog with a hint."""
    _seed_epic(home, "e-dep", title="Awaiting parent", status="not started")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO epic_dependencies(from_epic_id, to_epic_id, kind) VALUES (?, ?, 'hard')",
        ("e-parent", "e-dep"),
    )
    con.commit(); con.close()
    page = build_page(home, "kanban")
    cols = _cols(page)
    assert [c["id"] for c in cols["Backlog"]] == ["e-dep"]
    assert "blocked by deps" in cols["Backlog"][0]["sub"]


def test_escalated_lands_in_needs_you(home: Path) -> None:
    _seed_epic(home, "e-esc", title="Deadline passed", status="escalated")
    page = build_page(home, "kanban")
    cols = _cols(page)
    assert [c["id"] for c in cols["Needs you"]] == ["e-esc"]


def test_done_lands_in_done(home: Path) -> None:
    _seed_epic(home, "e-done", title="Shipped", status="done")
    page = build_page(home, "kanban")
    cols = _cols(page)
    assert [c["id"] for c in cols["Done"]] == ["e-done"]


def test_unknown_status_lands_in_needs_you(home: Path) -> None:
    """A status outside the documented domain lands in Needs you — never silently dropped.

    The epics CHECK constraint forbids unknown values; this test exercises
    the schema-bypassing escape hatch by recreating the table without the
    CHECK so the page proves it never silently drops a card on a future
    migration that loosens the constraint.
    """
    con = sqlite3.connect(home / "state.db")
    # Recreate the epics table without the CHECK so we can plant a row
    # outside the documented domain — the page is what we are testing,
    # not the constraint. Keep every column the page reads, just loosen
    # the status domain.
    con.execute("ALTER TABLE epics RENAME TO epics_checkeed")
    con.execute(
        "CREATE TABLE epics ("
        "  id TEXT PRIMARY KEY,"
        "  title TEXT NOT NULL,"
        "  status TEXT NOT NULL,"  # no CHECK clause
        "  kickoff_path TEXT,"
        "  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),"
        "  archived_at TEXT"
        ")"
    )
    con.execute("INSERT INTO epics(id, title, status) "
                "SELECT id, title, status FROM epics_checkeed")
    con.execute("DROP TABLE epics_checkeed")
    con.execute(
        "INSERT INTO epics(id, title, status) VALUES ('e-future', 'Future status', 'queued')"
    )
    con.commit(); con.close()
    cols = _cols(build_page(home, "kanban"))
    assert [c["id"] for c in cols["Needs you"]] == ["e-future"]


# ── do: the epic's own page ────────────────────────────────────────────────
#
# A card gets exactly one ``do`` (``sections.rs::render_columns`` wires the whole
# card to it and draws ``meta`` as text only), so repointing the card at the task
# page is zero-sum: the latest attempt's run and the kickoff file the card used to
# open directly now live on the page it opens. ``test_ide_pages_task.py`` pins
# that page; these two pin the card's half of the trade.


def test_do_opens_the_epics_own_page(home: Path, tmp_path: Path) -> None:
    """A run, a kickoff, or neither — the card opens the same page regardless."""
    kickoff = tmp_path / "kickoff-fixture.md"
    kickoff.write_text("# Sample kickoff\n", encoding="utf-8")
    _seed_epic(home, "e-run", title="Has a run")
    _seed_epic_attempt(home, "e-run", run_id="run-2", attempt=2)
    _seed_epic(home, "e-kick", title="Has a kickoff", kickoff_path=str(kickoff))
    _seed_epic(home, "e-naked", title="Naked epic")

    cards = {c["id"]: c for c in _cols(build_page(home, "kanban"))["Backlog"]}
    for epic_id in ("e-run", "e-kick", "e-naked"):
        assert cards[epic_id]["do"] == {"page": "task", "args": {"uid": epic_id}}


def test_the_page_a_card_opens_still_reaches_the_run_and_the_kickoff(
        home: Path, tmp_path: Path) -> None:
    """The shortcuts the card gave up moved onto the page, they did not vanish."""
    kickoff = tmp_path / "kickoff-fixture.md"
    kickoff.write_text("# Sample kickoff\n", encoding="utf-8")
    _seed_epic(home, "e-run", title="Has a run", kickoff_path=str(kickoff))
    _seed_epic_attempt(home, "e-run", run_id="run-1", attempt=1)
    _seed_epic_attempt(home, "e-run", run_id="run-2", attempt=2)

    task = build_page(home, "task", args={"uid": "e-run"})
    assert task["ok"] is True
    # Newest first, and the row opens the run the card used to.
    assert _section(task, "table", "Attempts")["rows"][0]["do"] == {
        "run": "run-2", "title": "e-run · attempt 2"}
    assert {a["label"]: a["do"] for a in task["actions"]}["Kickoff file"] == {
        "path": str(kickoff)}


# ── archived + cap + missing table ───────────────────────────────────────────


def test_archived_epics_are_hidden(home: Path) -> None:
    _seed_epic(home, "e-active", title="Active")
    _seed_epic(home, "e-archive", title="Archived",
               archived_at="2026-01-01T00:00:00Z")
    page = build_page(home, "kanban")
    ids = {c["id"] for c in _cols(page)["Backlog"]}
    assert "e-active" in ids
    assert "e-archive" not in ids


def test_each_column_caps_at_30(home: Path) -> None:
    for i in range(35):
        _seed_epic(home, f"e-{i:02d}", title=f"Epic {i:02d}",
                   status="not started", priority=i)
    page = build_page(home, "kanban")
    backlog = _cols(page)["Backlog"]
    assert len(backlog) == 30


def test_columns_sort_priority_desc_then_recency_desc(home: Path) -> None:
    """Higher priority comes first; same priority breaks by newer updated_at."""
    for i in range(5):
        _seed_epic(home, f"e-hi-{i}", title=f"High {i}", status="not started", priority=10)
        _seed_epic(home, f"e-lo-{i}", title=f"Low {i}", status="not started", priority=1)
    # Pin every updated_at so the secondary sort has a known order:
    # e-hi-1 (newest) → e-hi-2 → … → e-hi-0 (oldest); same shape for e-lo-*.
    con = sqlite3.connect(home / "state.db")
    for i in range(5):
        day = f"2026-{(i + 1):02d}-01T00:00:00Z"
        con.execute("UPDATE epics SET updated_at=? WHERE id=?", (day, f"e-hi-{i}"))
        con.execute("UPDATE epics SET updated_at=? WHERE id=?", (day, f"e-lo-{i}"))
    # Override e-hi-1 to be unambiguously the newest of its tier.
    con.execute("UPDATE epics SET updated_at='2026-12-31T00:00:00Z' WHERE id='e-hi-1'")
    con.commit(); con.close()
    backlog = _cols(build_page(home, "kanban"))["Backlog"]
    ids = [c["id"] for c in backlog]
    high_positions = [ids.index(c) for c in ids if c.startswith("e-hi-")]
    low_positions = [ids.index(c) for c in ids if c.startswith("e-lo-")]
    # Every high-priority card sorts before any low-priority one.
    assert max(high_positions) < min(low_positions)
    # Within high-priority, the more recently updated card comes first.
    high_ids = [c for c in ids if c.startswith("e-hi-")]
    assert high_ids[0] == "e-hi-1"


def test_missing_epics_table_renders_muted_line_and_ok_true(home: Path) -> None:
    """No epics table at all → the page still builds with ``ok: true`` and one muted line."""
    con = sqlite3.connect(home / "state.db")
    con.execute("DROP TABLE IF EXISTS epics")
    con.commit(); con.close()
    page = build_page(home, "kanban")
    assert page["ok"] is True
    assert page["tab"] == "tasks"
    # The Tasks section is a single list with a muted line — exactly one.
    tasks = _section(page, "list", "Tasks")
    assert len(tasks["items"]) == 1
    item = tasks["items"][0]
    assert item["t"] == "No epics table — initialise the project " \
                        "(``mini-ork init``) to see the Tasks column here."
    assert item["c"] == "sub"


# ── runs tab ────────────────────────────────────────────────────────────────


def test_runs_tab_matches_todays_four_column_board(home: Path) -> None:
    """``tab='runs'`` returns exactly the previous board — four columns, Done over 24 h."""
    now = int(time.time())
    _seed_run(home, "run-work", title="Working hard", status="executing", created=now - 60,
              cost=0.25)
    _seed_run(home, "run-done", title="All done", status="published", created=now - 3600)
    page = build_page(home, "kanban", tab="runs")
    assert page["tab"] == "runs"
    sec = _section(page, "columns")
    assert sec["full"] is True
    assert [c["title"] for c in sec["cols"]] == ["Working", "Needs you", "Failed", "Done"]


# ── JSON serialisation + the card schema ────────────────────────────────────


def test_cards_carry_only_documented_keys(home: Path) -> None:
    _seed_epic(home, "e-card", title="Card test", status="in progress")
    page = build_page(home, "kanban")
    card = _cols(page)["Working"][0]
    assert set(card) == {"id", "title", "sub", "state", "mark", "meta", "do"}
    json.dumps(page)  # the payload stays JSON-serialisable