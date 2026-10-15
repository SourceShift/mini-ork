"""``mini-ork board page kanban`` — imported tickets on the Tasks tab.

Acceptance matrix for the mirror the PM sources fill:

1. A Linear ``in_progress`` ticket lands in Working; a Jira ``in_review`` one
   in Needs you; ``backlog``/``todo`` in Backlog; ``done`` in Done.
2. The card's sub line carries the source's *native* state word, never only
   the normalized one.
3. A mirror row whose ``provider`` is ``local`` does not duplicate its epic.
4. ``canceled`` is not drawn; it is counted in the section note.
5. ``args['source']`` filters — ``linear`` shows only Linear cards,
   ``local`` shows epics only — and the header carries the ``S.set_args``
   filter buttons.
6. A recorded sync error renders a warn line with the source id and the error
   text, and the payload carries no token value.
7. With no external source enabled the tab still builds ``ok: true`` and one
   line names ``config/pm.yaml``.
8. ``do`` opens the linked run (``run:<run_id>`` link) with an ``agent`` chip,
   else the ticket's URL, else nothing.
9. Epics and tickets share one per-column cap and one priority-then-recency
   order, so a flood of low-priority tickets cannot push every epic off.

Everything is offline: the mirror is seeded through
``mini_ork.pm.store`` on a throwaway state.db, and the page is built with
:func:`mini_ork.ide_pages.build_page`.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mini_ork import scheduler
from mini_ork.ide_pages import build_page
from mini_ork.pm import store
from mini_ork.pm.model import (
    Issue,
    IssueState,
    LinkKind,
    Priority,
    ProviderIdentity,
)
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]

# A config with two external sources enabled — the ordinary, configured home.
_PM_TWO_SOURCES = """\
sources:
  local:
    enabled: true
  linear:
    enabled: true
    token_env: LINEAR_API_KEY
  jira:
    enabled: true
    token_env: JIRA_API_TOKEN
"""

# No external source: the epics-only project the empty state exists for.
_PM_LOCAL_ONLY = """\
sources:
  local:
    enabled: true
"""


# ── fixtures ────────────────────────────────────────────────────────────────


def _set_pm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    """Point ``$MINI_ORK_PM`` at a throwaway ``pm.yaml``.

    The page reads ``load_config()`` from the environment, so a test that did
    not pin this could read the developer's real config and make the empty
    state unreachable.
    """
    path = tmp_path / "pm.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_PM", str(path))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real mini-ork home with the migrations run and two PM sources enabled."""
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    scheduler.ensure_priority_column(str(h / "state.db"))
    scheduler.ensure_retry_schema(str(h / "state.db"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "proj"))
    _set_pm(monkeypatch, tmp_path, _PM_TWO_SOURCES)
    return h


def _seed_issue(home: Path, provider: str, key: str, *, title: str,
                state: IssueState, native_state: str,
                priority: Priority = Priority.MEDIUM, url: str | None = None,
                assignee: str | None = None,
                updated_at: str | None = None) -> None:
    """One mirror row, written exactly the way an adapter's sync writes it."""
    issue = Issue(
        identity=ProviderIdentity(provider=provider, key=key, url=url),
        title=title,
        state=state,
        native_state=native_state,
        priority=priority,
        assignee=assignee,
        updated_at=updated_at,
    )
    store.upsert_issues(provider, [issue], db=str(home / "state.db"))


def _seed_epic(home: Path, epic_id: str, *, title: str | None = None,
               status: str = "not started", priority: int = 0) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO epics(id, title, status, kickoff_path, priority, "
        "created_at, updated_at) VALUES (?,?,?,?,?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (epic_id, title or epic_id, status, None, priority),
    )
    con.commit(); con.close()


# ── helpers ─────────────────────────────────────────────────────────────────


def _section(page: dict[str, Any], kind: str, title: str | None = None) -> dict[str, Any]:
    for s in page["sections"]:
        if s["type"] == kind and (title is None or s["title"] == title):
            return s
    raise AssertionError(f"no {kind} section {title!r} in "
                         f"{[s['title'] for s in page['sections']]}")


def _cols(page: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    sec = _section(page, "columns", "Tasks")
    return {c["title"]: list(c["cards"]) for c in sec["cols"]}


def _card_title_has(page: dict[str, Any], needle: str) -> bool:
    for cards in _cols(page).values():
        for card in cards:
            if needle in card["title"]:
                return True
    return False


# ── state → column mapping ─────────────────────────────────────────────────


def test_linear_in_progress_lands_in_working(home: Path) -> None:
    _seed_issue(home, "linear", "ENG-1", title="Fix the retry",
                state=IssueState.IN_PROGRESS, native_state="In Progress")
    cols = _cols(build_page(home, "kanban"))
    assert [c["id"] for c in cols["Working"]] == ["linear:ENG-1"]
    assert cols["Backlog"] == [] and cols["Needs you"] == [] and cols["Done"] == []


def test_jira_in_review_lands_in_needs_you(home: Path) -> None:
    _seed_issue(home, "jira", "ENG-2", title="Under review",
                state=IssueState.IN_REVIEW, native_state="In Review")
    cols = _cols(build_page(home, "kanban"))
    assert [c["id"] for c in cols["Needs you"]] == ["jira:ENG-2"]


def test_blocked_and_backlog_and_done_land_where_the_mapping_says(home: Path) -> None:
    _seed_issue(home, "jira", "A-1", title="Blocked thing",
                state=IssueState.BLOCKED, native_state="Blocked")
    _seed_issue(home, "jira", "A-2", title="Backlog thing",
                state=IssueState.BACKLOG, native_state="Backlog")
    _seed_issue(home, "jira", "A-3", title="Done thing",
                state=IssueState.DONE, native_state="Done")
    cols = _cols(build_page(home, "kanban"))
    assert [c["id"] for c in cols["Needs you"]] == ["jira:A-1"]
    assert [c["id"] for c in cols["Backlog"]] == ["jira:A-2"]
    assert [c["id"] for c in cols["Done"]] == ["jira:A-3"]


# ── native state word + title shape ────────────────────────────────────────


def test_native_state_word_shows_in_the_sub_line(home: Path) -> None:
    """The source's own word rides the sub line, never only the normalized one."""
    _seed_issue(home, "jira", "OPS-9", title="Latency spike",
                state=IssueState.IN_PROGRESS, native_state="In Development")
    card = _cols(build_page(home, "kanban"))["Working"][0]
    assert card["sub"] == "jira · In Development"
    assert "in_progress" not in card["sub"]


def test_title_leads_with_the_native_key(home: Path) -> None:
    _seed_issue(home, "linear", "ENG-42", title="Ship it",
                state=IssueState.IN_PROGRESS, native_state="In Progress")
    card = _cols(build_page(home, "kanban"))["Working"][0]
    assert card["title"] == "ENG-42 · Ship it"


def test_ticket_card_carries_a_source_chip(home: Path) -> None:
    _seed_issue(home, "linear", "ENG-7", title="Chip me",
                state=IssueState.IN_PROGRESS, native_state="In Progress")
    card = _cols(build_page(home, "kanban"))["Working"][0]
    assert any(m["t"] == "linear" for m in card["meta"])


def test_ticket_cards_carry_only_documented_keys(home: Path) -> None:
    _seed_issue(home, "linear", "ENG-8", title="Keys", state=IssueState.IN_PROGRESS,
                native_state="In Progress")
    card = _cols(build_page(home, "kanban"))["Working"][0]
    assert set(card) == {"id", "title", "sub", "state", "mark", "meta", "do"}
    json.dumps(build_page(home, "kanban"))  # stays JSON-serialisable


# ── local rows are not duplicated ──────────────────────────────────────────


def test_local_mirror_row_does_not_duplicate_its_epic(home: Path) -> None:
    """The epic is drawn from ``epics``; a ``local`` mirror row is skipped."""
    _seed_epic(home, "e-1", title="Own epic")
    _seed_issue(home, "local", "e-1", title="Own epic",
                state=IssueState.TODO, native_state="not started")
    backlog = _cols(build_page(home, "kanban"))["Backlog"]
    ids = [c["id"] for c in backlog]
    assert ids == ["e-1"]                       # the epic only, no "local:e-1"
    assert "local:e-1" not in ids


# ── canceled ───────────────────────────────────────────────────────────────


def test_canceled_ticket_is_not_drawn_and_is_counted(home: Path) -> None:
    _seed_issue(home, "jira", "OLD-1", title="Dropped work",
                state=IssueState.CANCELED, native_state="Won't Do")
    page = build_page(home, "kanban")
    assert not _card_title_has(page, "OLD-1")
    for cards in _cols(page).values():
        assert all(c["id"] != "jira:OLD-1" for c in cards)
    note = _section(page, "columns", "Tasks")["note"]
    assert "1 canceled ticket(s)" in note


# ── source filter ──────────────────────────────────────────────────────────


def test_source_filter_shows_only_that_provider(home: Path) -> None:
    _seed_issue(home, "linear", "LIN-1", title="Linear work",
                state=IssueState.TODO, native_state="Todo")
    _seed_issue(home, "jira", "JIR-1", title="Jira work",
                state=IssueState.TODO, native_state="To Do")
    page = build_page(home, "kanban", args={"source": "linear"})
    titles = [c["title"] for cards in _cols(page).values() for c in cards]
    assert any("LIN-1" in t for t in titles)
    assert not any("JIR-1" in t for t in titles)


def test_source_local_shows_epics_only(home: Path) -> None:
    _seed_epic(home, "e-only", title="Just an epic")
    _seed_issue(home, "linear", "LIN-2", title="Ignored",
                state=IssueState.TODO, native_state="Todo")
    page = build_page(home, "kanban", args={"source": "local"})
    for cards in _cols(page).values():
        for c in cards:
            assert not str(c["id"]).startswith("linear:")


def test_header_has_source_filter_buttons(home: Path) -> None:
    _seed_issue(home, "linear", "LIN-3", title="Shows the button",
                state=IssueState.TODO, native_state="Todo")
    page = build_page(home, "kanban")
    actions = {json.dumps(a.get("do"), sort_keys=True): a for a in page["actions"]}
    assert json.dumps({"set": {"source": "all"}}, sort_keys=True) in actions
    assert json.dumps({"set": {"source": "local"}}, sort_keys=True) in actions
    assert json.dumps({"set": {"source": "linear"}}, sort_keys=True) in actions


def test_sync_tickets_button_dispatches_intake_dry_run(home: Path) -> None:
    page = build_page(home, "kanban")
    sync = next((a for a in page["actions"] if a["label"] == "Sync tickets"), None)
    assert sync is not None, page["actions"]
    assert sync["do"] == {"cli": ["intake", "once", "--dry-run"]}


# ── freshness + empty state ────────────────────────────────────────────────


def test_sync_error_renders_a_warn_line_without_a_token(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_secret_value_xyz")
    _seed_issue(home, "linear", "LIN-4", title="Stale",
                state=IssueState.TODO, native_state="Todo")
    store.record_sync_error("linear", "linear: request failed (401 Unauthorized)",
                            db=str(home / "state.db"))
    page = build_page(home, "kanban")
    items = _section(page, "list", "Sources")["items"]
    warn = next((i for i in items if i["t"] == "linear"), None)
    assert warn is not None, items
    assert "401" in warn["sub"]
    assert "lin_secret_value_xyz" not in json.dumps(page)


def test_no_external_source_still_builds_and_names_pm_yaml(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_pm(monkeypatch, tmp_path, _PM_LOCAL_ONLY)
    page = build_page(home, "kanban")
    assert page["ok"] is True
    items = _section(page, "list", "Sources")["items"]
    assert len(items) == 1
    text = items[0]["t"]
    assert "config/pm.yaml" in text
    assert "enabled: true" in text
    assert "token_env" in text


# ── do: the ticket's own page ──────────────────────────────────────────────
#
# A card gets exactly one ``do``, so repointing it at the task page is zero-sum:
# the linked run and the source URL the card used to open directly are now on the
# page it opens (``task.py`` draws both as its own actions). The chip that says
# mini-ork is working the ticket stays on the card — ``meta`` is text, so it costs
# no click.


def test_do_opens_the_tickets_own_page(home: Path) -> None:
    """A run link, a url, or neither — the card opens the same page regardless."""
    _seed_issue(home, "linear", "ENG-7", title="Worked by mini-ork",
                state=IssueState.IN_PROGRESS, native_state="In Progress")
    store.link("linear:ENG-7", "run:run-abc123", LinkKind.RUN, db=str(home / "state.db"))
    _seed_issue(home, "jira", "JIR-9", title="Nothing to open",
                state=IssueState.IN_PROGRESS, native_state="In Progress")

    cards = {c["id"]: c for c in _cols(build_page(home, "kanban"))["Working"]}
    assert cards["linear:ENG-7"]["do"] == {"page": "task", "args": {"uid": "linear:ENG-7"}}
    assert cards["jira:JIR-9"]["do"] == {"page": "task", "args": {"uid": "jira:JIR-9"}}


def test_a_run_link_still_adds_the_agent_chip(home: Path) -> None:
    """The chip is derived from the link, not from the card's ``do``."""
    _seed_issue(home, "linear", "ENG-7", title="Worked by mini-ork",
                state=IssueState.IN_PROGRESS, native_state="In Progress")
    store.link("linear:ENG-7", "run:run-abc123", LinkKind.RUN, db=str(home / "state.db"))
    card = _cols(build_page(home, "kanban"))["Working"][0]
    assert any(m["t"] == "agent" for m in card["meta"])


def test_the_page_a_ticket_card_opens_still_reaches_the_run_and_the_url(home: Path) -> None:
    """The shortcuts the card gave up moved onto the page, they did not vanish."""
    _seed_issue(home, "linear", "ENG-7", title="Worked by mini-ork",
                state=IssueState.IN_PROGRESS, native_state="In Progress",
                url="https://linear.app/acme/issue/ENG-7")
    store.link("linear:ENG-7", "run:run-abc123", LinkKind.RUN, db=str(home / "state.db"))

    task = build_page(home, "task", args={"uid": "linear:ENG-7"})
    assert task["ok"] is True
    assert {a["label"]: a["do"] for a in task["actions"]}["Open in linear"] == {
        "url": "https://linear.app/acme/issue/ENG-7"}
    links = _section(task, "list", "Links")["items"]
    assert links[0]["acts"][0]["do"]["run"] == "run-abc123"


# ── shared cap + order ─────────────────────────────────────────────────────


def test_shared_cap_and_order_keeps_prioritised_epics(home: Path) -> None:
    """One cap and one order over the merge: a ticket flood cannot drop every epic."""
    for i in range(5):
        _seed_epic(home, f"e-{i}", title=f"Epic {i}", status="not started", priority=5)
    for i in range(40):
        _seed_issue(home, "jira", f"J-{i}", title=f"Ticket {i}",
                    state=IssueState.TODO, native_state="To Do",
                    priority=Priority.NONE)
    backlog = _cols(build_page(home, "kanban"))["Backlog"]
    assert len(backlog) == 30                              # the shared cap
    kept = {c["id"] for c in backlog}
    assert {f"e-{i}" for i in range(5)} <= kept            # every epic survives


def test_a_large_epic_backlog_does_not_hide_every_ticket(home: Path) -> None:
    """The inverse flood: 35 prioritised epics used to fill Backlog's 30 slots,
    so an imported ticket never showed on the All view (live, 2026-10-15)."""
    for i in range(35):
        _seed_epic(home, f"e-{i}", title=f"Epic {i}", status="not started", priority=5)
    _seed_issue(home, "github-cn", "SourceShift/ContextNest#205", title="Update API",
                state=IssueState.BACKLOG, native_state="open", priority=Priority.NONE)
    backlog = _cols(build_page(home, "kanban"))["Backlog"]
    ids = [c["id"] for c in backlog]
    assert len(backlog) == 30
    assert "github-cn:SourceShift/ContextNest#205" in ids
    assert sum(1 for i in ids if ":" not in i) == 29


def test_one_source_alone_keeps_the_plain_priority_order(home: Path) -> None:
    for i in range(3):
        _seed_epic(home, f"e-{i}", title=f"Epic {i}", status="not started", priority=i)
    backlog = _cols(build_page(home, "kanban"))["Backlog"]
    assert [c["id"] for c in backlog] == ["e-2", "e-1", "e-0"]


# ── the Sources tab ────────────────────────────────────────────────────────
#
# The connect-from-the-UI surface: declared sources with an explicit Test, the
# add boxes for what is not declared yet, and — the property the whole epic
# turns on — no credential VALUE anywhere in the payload. The pane's section is
# titled "PM sources" so it does not collide with the Tasks tab's "Sources"
# sync list that the tests above read.

_EMPTY_PM = "sources: {}\n"


def _pane_rows(page: dict[str, Any]) -> list[dict[str, Any]]:
    return _section(page, "list", "PM sources")["items"]


def _pane_row(page: dict[str, Any], source_id: str) -> dict[str, Any]:
    row = next((i for i in _pane_rows(page) if i["t"] == source_id), None)
    assert row is not None, [i["t"] for i in _pane_rows(page)]
    return row


def _acts(page: dict[str, Any], source_id: str) -> dict[str, dict[str, Any]]:
    return {a["label"]: a for a in _pane_row(page, source_id)["acts"]}


def _sources_page(home: Path, **args: Any) -> dict[str, Any]:
    return build_page(home, "kanban", args=args, tab="sources")


def test_the_sources_tab_builds_and_lists_every_declared_source(home: Path) -> None:
    page = _sources_page(home)
    assert page["ok"] is True
    assert [row["t"] for row in _pane_rows(page)] == ["jira", "linear", "local"]


def test_a_source_row_offers_an_explicit_test_button(home: Path) -> None:
    """Test is a button, never a side effect of opening the pane."""
    acts = _acts(_sources_page(home), "linear")
    assert acts["Test"]["do"] == {"cli": ["pm", "test", "linear"]}


def test_a_source_row_offers_remove_and_local_does_not(home: Path) -> None:
    page = _sources_page(home)
    assert _acts(page, "linear")["Remove"]["do"] == {"cli": ["pm", "remove", "linear"]}
    assert "Remove" not in _acts(page, "local")     # mini-ork's own epics are not a source


def test_an_undeclared_addable_source_gets_an_add_box(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_pm(monkeypatch, tmp_path, _PM_LOCAL_ONLY)
    boxes = _section(_sources_page(home), "composer", None)
    assert boxes["cli"] == ["pm", "add", "linear", "--set"]  # first addable, in order


def test_every_addable_source_gets_its_own_add_box(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_pm(monkeypatch, tmp_path, _PM_LOCAL_ONLY)
    page = _sources_page(home)
    prefixes = [s["cli"] for s in page["sections"] if s["type"] == "composer"]
    assert ["pm", "add", "linear", "--set"] in prefixes
    assert ["pm", "add", "jira", "--set"] in prefixes


def test_a_declared_source_gets_no_add_box(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = _sources_page(home)
    prefixes = [s["cli"] for s in page["sections"] if s["type"] == "composer"]
    assert not any(prefix[:3] == ["pm", "add", "linear"] for prefix in prefixes)


def test_the_sources_tab_never_carries_a_credential_value(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value in a page payload is a value in a log. The pane holds key NAMES."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_sentinel_value_never_in_a_payload")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira_sentinel_value_never_in_a_payload")
    store.record_sync_error("linear", "linear: request failed (401 Unauthorized)",
                            db=str(home / "state.db"))
    payload = json.dumps(_sources_page(home))
    assert "lin_sentinel_value_never_in_a_payload" not in payload
    assert "jira_sentinel_value_never_in_a_payload" not in payload
    assert "LINEAR_API_KEY" in payload                 # the name is fine; the value is not


def test_a_source_with_a_sync_error_is_marked_and_scrubbed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_sentinel_value")
    store.record_sync_error("linear", "linear: request failed (401 Unauthorized)",
                            db=str(home / "state.db"))
    row = _pane_row(_sources_page(home), "linear")
    assert row["m"] == "!"                             # the warn mark
    assert "401" in row["sub"]
    assert "lin_sentinel_value" not in json.dumps(row)


def test_the_sources_tab_says_what_to_do_when_nothing_is_declared(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_pm(monkeypatch, tmp_path, _EMPTY_PM)
    page = _sources_page(home)
    assert page["ok"] is True
    rows = _pane_rows(page)
    assert len(rows) == 1                              # the one muted hint
    assert "config/pm.yaml" in rows[0]["t"]


def test_the_default_tab_is_still_the_tasks_board(home: Path) -> None:
    """Adding the Sources pane must not move the default tab out from under it."""
    page = build_page(home, "kanban")
    assert page["tab"] == "tasks"
    _section(page, "columns", "Tasks")


def test_the_sources_tab_is_advertised_in_the_tab_strip(home: Path) -> None:
    tabs = {t["key"]: t["label"] for t in build_page(home, "kanban")["tabs"]}
    assert tabs.get("sources") == "Sources"
