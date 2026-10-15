"""``task`` — one epic's, or one mirrored ticket's, own tab.

The Board's cards open here (``spec.open_task``) and the IDE gives each ``uid``
its own tab, so this page owes the shared page tests in
``test_ide_pages_actions.py`` two things they cannot check: it must round-trip
*real* rows out of ``state.db`` (that file only ever builds the empty home, so
``_page_args_for("task") == {}`` means it exercises just the "no card selected"
branch), and it must never hand ``scheduler``/``epics`` an argument their
parsers reject.

That second one is not hypothetical. ``board.rs::run_action`` appends
``--home <home>`` at the END of every ``cli`` action's argv, and both
``scheduler.main``'s flag loop and ``epics._retry`` reject an unknown trailing
flag with rc 2 — silently, because the action path runs with no TTY. The last
test here runs those two heads with the flag appended and asserts rc 2, which
is what makes the ``home=False`` in ``task._actions`` load-bearing rather than
decorative.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import PAGES, build_page

#: A real adapter failure. The page's whole job on the ticket branch is to
#: survive one of these, so the tests that fake an unreachable source raise the
#: type an unreachable source raises.
from mini_ork.pm.provider import ProviderError  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    from mini_ork import scheduler
    from mini_ork.stores import migrate as mig

    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    db = str(h / "state.db")
    rc, _out, err = mig.init_db(db=db, root=str(REPO))
    assert rc == 0, err
    del _out
    # ``epics.priority`` and the ``attempts``/``recipe``/``held_reason`` columns
    # plus ``epic_attempts`` are runtime migrations, not in 0001_core.sql — the
    # same two ensurers the scheduler and ``epics`` run before either can read a
    # row. Seeding through them is what keeps this test honest about the schema
    # the page actually meets in production.
    scheduler.ensure_priority_column(db)
    scheduler.ensure_retry_schema(db)
    return h


def _sql(home: Path, *statements: tuple[str, tuple]) -> None:
    con = sqlite3.connect(home / "state.db")
    try:
        for sql, params in statements:
            con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _seed_epic(home: Path, epic_id: str, *, status: str = "escalated",
               held_reason: str | None = None, kickoff_path: str | None = None) -> None:
    _sql(home, (
        "INSERT INTO epics (id, title, status, recipe, lane, priority, attempts, "
        "max_attempts, held_reason, kickoff_path, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (epic_id, f"Epic {epic_id}", status, "epic-runner", "codex", 2, 3, 3,
         held_reason, kickoff_path, "2026-10-01T00:00:00.000Z", "2026-10-02T00:00:00.000Z"),
    ))


def _seed_attempt(home: Path, epic_id: str, attempt: int, run_id: str,
                  *, outcome: str = "failed") -> None:
    _sql(home, (
        "INSERT INTO epic_attempts (epic_id, attempt, run_id, recipe, verdict, outcome, "
        "reason, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (epic_id, attempt, run_id, "epic-runner", "unknown", outcome,
         "verdict=unknown rc=1", "2026-10-09T17:42:39.657Z", "2026-10-09T17:50:00.000Z"),
    ))


def _seed_dependency(home: Path, from_id: str, to_id: str, kind: str = "hard") -> None:
    _sql(home, (
        "INSERT INTO epic_dependencies (from_epic_id, to_epic_id, kind, created_at) "
        "VALUES (?,?,?,?)",
        (from_id, to_id, kind, "2026-10-01T00:00:00.000Z"),
    ))


def _seed_inbox(home: Path, epic_id: str, body: str) -> None:
    _sql(home, (
        "INSERT INTO inbox (epic_id, kind, body_md, opened_at) VALUES (?,?,?,?)",
        (epic_id, "escalation", body, "2026-10-09T17:45:00.000Z"),
    ))


def _seed_ticket(home: Path, uid: str, *, provider: str = "acme", native_key: str = "ENG-1",
                 state: str = "in_progress", url: str = "https://acme.example/ENG-1") -> None:
    now = "2026-10-10T00:00:00.000Z"
    _sql(home, (
        "INSERT INTO pm_issues (uid, provider, native_key, title, state, native_state, "
        "priority, native_priority, url, assignee, labels, parent_uid, project, "
        "updated_at, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, provider, native_key, "Ticket one", state, "In Progress", 3, 2, url,
         "someone", '["billing","q4"]', None, "Project X", "2026-10-09T00:00:00.000Z", now),
    ))


def _cache_body(home: Path, uid: str, *, description: str | None = None,
                created_at: str | None = None, body_synced_at: str | None = None,
                body_error: str | None = None) -> None:
    """Write 0072's body-cache columns directly, the way a past read left them."""
    _sql(home, (
        "UPDATE pm_issues SET description=?, created_at=?, body_synced_at=?, body_error=? "
        "WHERE uid=?",
        (description, created_at, body_synced_at, body_error, uid),
    ))


class FakeAcme:
    """A source that answers from memory — no registry config, no socket.

    Registered per test under the id the seeded mirror row names, so the page
    resolves exactly the adapter the test means and nothing else. ``raising``
    is a real :class:`ProviderError` (what an unreachable source produces);
    ``comments_error`` is separate because a thread is best-effort while the
    body is not.
    """

    id = "acme"
    label = "Acme"

    def __init__(self, issue=None, comments=(), *, raising: BaseException | None = None,
                 comments_error: BaseException | None = None) -> None:
        from mini_ork.pm.provider import Capabilities

        self.capabilities = Capabilities()   # a read-only source
        self.issue = issue
        self.comment_list = list(comments)
        self.raising = raising
        self.comments_error = comments_error
        self.calls: list[str] = []

    def get(self, uid: str):
        self.calls.append("get")
        if self.raising is not None:
            raise self.raising
        return self.issue

    def comments(self, uid: str):
        self.calls.append("comments")
        if self.comments_error is not None:
            raise self.comments_error
        return self.comment_list


@pytest.fixture()
def acme():
    """Register a fake ``acme`` source for one test, and always take it back out.

    The registry is process-global: a fake left behind would be resolved by
    every later test that seeds an ``acme:`` mirror row.
    """
    from mini_ork.pm import registry

    def install(provider):
        registry.register_provider(provider, replace=True)
        return provider

    yield install
    registry.unregister_provider("acme")


def _an_issue(uid: str = "acme:ENG-1", **overrides):
    from mini_ork.pm.model import Issue, IssueState, Priority, ProviderIdentity

    fields = {
        "identity": ProviderIdentity(provider="acme", key="ENG-1", project="Project X",
                                     url="https://acme.example/ENG-1"),
        "title": "Ticket one",
        "state": IssueState.IN_PROGRESS,
        "native_state": "In Progress",
        "description": "## Problem\n\nThe widget falls over on start.\n",
        "created_at": "2026-10-05T09:00:00.000Z",
        "updated_at": "2026-10-09T00:00:00.000Z",
        "priority": Priority.HIGH,
    }
    fields.update(overrides)
    return Issue(**fields)


def _a_comment(author: str, created_at: str, body: str = "a note"):
    from mini_ork.pm.model import Comment

    return Comment(issue_uid="acme:ENG-1", author=author, body=body, created_at=created_at)


def _flat_sections(page: dict) -> list[dict]:
    """Every section, with the "a ``guarded`` result must be ``extend``-ed" rule enforced.

    ``S.guarded`` always returns a *list*, so a page that ``append``-s it nests
    an array inside ``sections`` and the Rust reader's ``sections[i]["type"]``
    blows up on the client, not here. Asserting flatness in the builder's own
    test is the only place it is cheap to catch.
    """
    sections = page.get("sections") or []
    nested = [i for i, s in enumerate(sections) if isinstance(s, list)]
    assert not nested, f"nested section lists at {nested} — use extend, not append"
    return sections


def _section(page: dict, type_: str, title: str | None = None) -> dict:
    for section in _flat_sections(page):
        if section.get("type") == type_ and (title is None or section.get("title") == title):
            return section
    raise AssertionError(f"no {type_} section {title!r} in "
                         f"{[(s.get('type'), s.get('title')) for s in _flat_sections(page)]}")


def _actions(page: dict) -> dict[str, dict]:
    return {str(a.get("label")): a for a in page.get("actions") or []}


def test_task_is_registered_as_its_own_tab() -> None:
    assert PAGES["task"] == "Task"


def test_no_uid_asks_for_a_card(home: Path) -> None:
    page = build_page(home, "task", args={})
    assert page["ok"] is True
    assert page["sub"] == "No task selected."
    # The tab reads as a plain "Task" until a card names one.
    assert page["label"] == "Task"
    assert _section(page, "list", "Task")["items"]


def test_a_project_without_state_db_still_pages(home: Path) -> None:
    page = build_page(home.parent, "task", args={"uid": "anything"})
    assert page["ok"] is True
    assert "no state.db" in page["sub"]


def test_an_unknown_uid_is_not_found_not_an_error(home: Path) -> None:
    """Neither an epic nor a mirrored ticket: a plain answer, no exception.

    ``pm.store.row`` runs its own ``ensure_schema`` first, so a home that has
    never mirrored anything returns ``None`` rather than raising — the page must
    therefore reach "Not found" with no error attached.
    """
    page = build_page(home, "task", args={"uid": "no-such-thing"})
    assert page["ok"] is True
    assert page["sub"] == "Not found in this project."
    assert not page["errors"], page["errors"]


def test_epic_page_carries_its_actions_history_and_edges(home: Path) -> None:
    _seed_epic(home, "e1", status="escalated")
    _seed_attempt(home, "e1", 1, "run-1")
    _seed_attempt(home, "e1", 2, "run-2")
    _seed_dependency(home, "e1", "e2")          # e1 blocks e2
    _seed_dependency(home, "e3", "e1", "soft")  # e3 blocks e1
    _seed_inbox(home, "e1", "the reviewer never answered")

    page = build_page(home, "task", args={"uid": "e1"})

    assert page["ok"] is True
    assert page["label"] == "Epic e1"           # the tab reads as the epic
    assert page["sub"].startswith("Epic · escalated")

    # The escalation callout and the open inbox row both land above the facts.
    callouts = [s for s in _flat_sections(page) if s.get("type") == "callout"]
    assert [c["title"] for c in callouts][:2] == ["Escalated", "Needs you · escalation"]
    assert "the reviewer never answered" in callouts[1]["text_md"]

    facts = _section(page, "kv", "Epic")["items"]
    by_label = {item["k"]: item for item in facts}
    assert by_label["Status"]["v"] == "escalated"
    assert by_label["Attempts"]["v"] == "3 of 3"
    assert by_label["Created"]["v"].startswith("2026-10-01T00:00:00.000Z")

    attempts = _section(page, "table", "Attempts")["rows"]
    assert [r["cells"][0]["t"] for r in attempts] == ["#2", "#1"]  # newest first
    assert attempts[0]["cells"][1]["t"] == "failed"
    assert attempts[0]["do"] == {"run": "run-2", "title": "e1 · attempt 2"}

    deps = _section(page, "table", "Dependencies")["rows"]
    edges = {r["cells"][1]["t"]: r for r in deps}
    assert edges["e2"]["cells"][0]["t"] == "blocks"
    assert edges["e3"]["cells"][0]["t"] == "blocked by"
    assert edges["e3"]["cells"][2]["t"] == "soft"
    assert edges["e2"]["do"] == {"page": "task", "args": {"uid": "e2"}}

    labels = _actions(page)
    assert labels["Dispatch now"]["do"]["cli"] == ["scheduler", "--once", "--epic", "e1"]
    assert labels["Dispatch now"]["do"]["home"] is False
    assert labels["Dry run"]["do"]["cli"][-1] == "--dry-run"
    assert labels["Retry"]["do"]["cli"] == ["epics", "retry", "e1"]
    assert labels["Retry"]["do"]["home"] is False
    # No kickoff_path recorded, so there is nothing to open.
    assert "Kickoff file" not in labels


def test_epic_page_offers_the_kickoff_file_only_when_it_is_on_disk(home: Path) -> None:
    kickoff = home / "kickoff.md"
    kickoff.write_text("# Kickoff\n\nDo the thing.\n", encoding="utf-8")
    _seed_epic(home, "e1", status="blocked", held_reason="waiting on you",
               kickoff_path=str(kickoff))

    page = build_page(home, "task", args={"uid": "e1"})

    assert page["ok"] is True
    # A held epic gets the hold callout, and Retry — not the escalation copy.
    assert _flat_sections(page)[0]["title"] == "Held by the scheduler"
    labels = _actions(page)
    assert labels["Retry"]["do"]["cli"] == ["epics", "retry", "e1"]
    assert labels["Kickoff file"]["do"] == {"path": str(kickoff)}
    markdown = _section(page, "markdown", "kickoff.md")
    assert "Do the thing." in markdown["text"]


def test_epic_page_says_so_when_a_recorded_kickoff_vanished(home: Path) -> None:
    _seed_epic(home, "e1", status="escalated", kickoff_path="/nowhere/kickoff.md")
    page = build_page(home, "task", args={"uid": "e1"})

    assert page["ok"] is True
    # The row still names the path — the page must not pretend there is no such
    # record, it must say the file is gone.
    kickoff = _section(page, "list", "Kickoff")
    assert "No kickoff file on disk" in kickoff["items"][0]["t"]
    assert "/nowhere/kickoff.md" in kickoff["items"][0]["t"]
    assert "Kickoff file" not in _actions(page)


def test_ticket_page_carries_its_facts_links_and_actions(home: Path) -> None:
    """The mirror half of the page, with no adapter registered for the source."""
    _seed_ticket(home, "acme:ENG-1")
    _sql(home, (
        "INSERT INTO pm_links (from_uid, to_uid, kind, created_at) VALUES (?,?,?,?)",
        ("acme:ENG-1", "run:run-1791567759-44925", "run", "2026-10-10T00:00:00.000Z"),
    ))

    page = build_page(home, "task", args={"uid": "acme:ENG-1"})

    assert page["ok"] is True
    assert page["label"] == "Ticket one"
    assert page["sub"] == "Ticket · acme:ENG-1"

    facts = {item["k"]: item for item in _section(page, "kv", "Ticket")["items"]}
    assert facts["Source"]["v"] == "acme:ENG-1"
    assert facts["State"]["v"] == "in_progress"
    # The source's own word is kept as a note, never as the value.
    assert "In Progress" in facts["State"]["sub"]
    assert facts["Priority"]["v"] == "high"     # normalized, not native_priority (2)
    assert facts["Labels"]["v"] == "billing, q4"

    links = _section(page, "list", "Links")["items"]
    assert links[0]["t"].startswith("run run-1791567759-44925")
    assert links[0]["acts"][0]["do"] == {"run": "run-1791567759-44925",
                                         "title": "acme:ENG-1 · run"}

    labels = _actions(page)
    assert labels["Open in acme"]["do"] == {"url": "https://acme.example/ENG-1"}
    assert "Dispatch now" not in labels         # a ticket is not an epic


def test_ticket_page_degrades_when_the_source_was_never_registered(home: Path) -> None:
    """No adapter, no cached body: a callout that says so, not a traceback.

    ``UnknownProvider`` is a ``KeyError``, not a ``ProviderError`` — the most
    common misconfiguration in a checkout (a ticket whose Linear token is not
    set up) is also the one a naive catch would let blank the tab.
    """
    _seed_ticket(home, "acme:ENG-1")

    page = build_page(home, "task", args={"uid": "acme:ENG-1"})

    assert page["ok"] is True
    sections = _flat_sections(page)
    assert sections[0]["type"] == "callout"
    assert sections[0]["title"] == "Source unreachable"
    # Nothing was ever cached, so a markdown section would be a lie.
    assert "no body has ever been read" in sections[0]["text_md"]
    assert "unknown pm provider 'acme'" in sections[0]["text_md"]
    assert not [s for s in sections if s.get("type") == "markdown"]


def test_ticket_page_reads_live_and_renders_body_created_and_comments(home: Path,
                                                                     acme) -> None:
    """The adapter answers: body, created date and thread, oldest first."""
    _seed_ticket(home, "acme:ENG-1")
    adapter = acme(FakeAcme(_an_issue(), [
        # Deliberately newest-first: the page must order them, not trust them.
        _a_comment("bob", "2026-10-07T10:00:00.000Z", "fixed upstream"),
        _a_comment("ada", "2026-10-06T10:00:00.000Z", "reproduced on 3.12"),
    ]))

    page = build_page(home, "task", args={"uid": "acme:ENG-1"})

    assert page["ok"] is True
    assert adapter.calls == ["get", "comments"]
    assert _flat_sections(page)[0]["title"] == "Read live"

    body = _section(page, "markdown", "Description")
    assert "The widget falls over on start." in body["text"]

    thread = _section(page, "list", "Comments")["items"]
    assert [i["t"] for i in thread] == ["ada", "bob"]         # oldest first
    assert thread[0]["sub"].startswith("2026-10-06T10:00:00.000Z")
    assert "ago" in thread[0]["sub"]
    assert "reproduced on 3.12" not in thread[0]["t"]          # body is not the title

    facts = {item["k"]: item for item in _section(page, "kv", "Ticket")["items"]}
    # Created sits beside Updated, from the live read rather than the cache.
    assert facts["Created"]["v"].startswith("2026-10-05T09:00:00.000Z")
    assert facts["Updated"]["v"].startswith("2026-10-09T00:00:00.000Z")

    # And the body it read is now cached, so the next open survives the source.
    import sqlite3

    con = sqlite3.connect(home / "state.db")
    try:
        cached = con.execute(
            "SELECT description, created_at, body_synced_at, body_error FROM pm_issues "
            "WHERE uid = 'acme:ENG-1'").fetchone()
    finally:
        con.close()
    assert cached[0] == _an_issue().description
    assert cached[1] == "2026-10-05T09:00:00.000Z"
    assert cached[2]                                    # stamped as a live read
    assert cached[3] is None                            # the source answered


def test_ticket_page_renders_the_cached_body_and_why_when_the_source_is_down(
        home: Path, acme) -> None:
    """A failing source degrades to the last body read, plus the reason."""
    _seed_ticket(home, "acme:ENG-1")
    _cache_body(home, "acme:ENG-1", description="the old body",
                created_at="2026-10-05T09:00:00.000Z",
                body_synced_at="2026-10-09T00:00:00.000Z")
    from mini_ork.pm.provider import ProviderError

    acme(FakeAcme(raising=ProviderError("linear is down")))

    page = build_page(home, "task", args={"uid": "acme:ENG-1"})

    assert page["ok"] is True
    sections = _flat_sections(page)
    callout = sections[0]
    assert callout["title"] == "Source unreachable"
    # The three questions, in order: which source, how stale, why.
    assert "acme:ENG-1" in callout["text_md"]
    assert "2026-10-09T00:00:00.000Z" in callout["text_md"]
    assert "ago" in callout["text_md"]
    assert "ProviderError: linear is down" in callout["text_md"]

    # The cached body is drawn, and labelled as cached rather than live.
    cached = _section(page, "markdown", "Description (cached)")
    assert cached["text"] == "the old body"
    assert not [s for s in sections if s.get("title") == "Description"]


def test_ticket_page_never_cached_source_down_is_still_ok(home: Path, acme) -> None:
    """Nothing cached, source down: mirror facts and a callout, ok true."""
    _seed_ticket(home, "acme:ENG-1")
    from mini_ork.pm.provider import ProviderError

    acme(FakeAcme(raising=ProviderError("timed out")))

    page = build_page(home, "task", args={"uid": "acme:ENG-1"})

    assert page["ok"] is True
    assert not page["errors"], page["errors"]
    sections = _flat_sections(page)
    assert sections[0]["title"] == "Source unreachable"
    assert "no body has ever been read" in sections[0]["text_md"]
    assert not [s for s in sections if s.get("type") == "markdown"]
    assert _section(page, "kv", "Ticket")["items"]      # the facts are still there


def test_ticket_page_without_a_url_offers_no_dead_link(home: Path) -> None:
    _seed_ticket(home, "acme:ENG-2", native_key="ENG-2", url="")
    page = build_page(home, "task", args={"uid": "acme:ENG-2"})

    assert page["ok"] is True
    assert "Open in" not in " ".join(_actions(page))


def test_the_two_verbs_the_page_dispatches_into_reject_an_appended_home(home: Path) -> None:
    """The premise behind ``home=False``: an appended ``--home`` exits 2.

    ``board.rs::run_action`` appends ``--home <home>`` unless the action's dict
    says ``"home": false``. Both heads this page dispatches into resolve their
    own home from ``MINI_ORK_HOME``/``MINI_ORK_DB`` and have no ``--home`` flag,
    so the appended one is an unknown *positional*: ``scheduler``'s flag loop
    bails on it and ``epics._retry`` lists it as an unknown argument. Neither
    call reaches the db, so this asserts the contract without side effects.

    If either of these ever returns something other than 2 the page's
    ``home=False`` is no longer required — but while it does, dropping the flag
    would silently turn every "Dispatch now" into an rc-2 no-op.
    """
    from mini_ork import scheduler
    from mini_ork.cli import epics

    db = str(home / "state.db")
    assert scheduler.main(["--once", "--epic", "e1", "--home", db], db=db,
                          home=str(home)) == 2
    assert epics.main(["retry", "e1", "--home", db], db=db) == 2


def test_the_actions_this_page_emits_really_do_resolve_their_home(home: Path) -> None:
    """The other half: with ``home=False`` the verbs still find the right db.

    ``board.rs`` always exports ``MINI_ORK_HOME``, so the verb's own env read is
    what locates the state — prove ``epics retry`` works with only that env and
    no ``--home``, on a real escalated row.
    """
    import os

    from mini_ork.cli import epics

    _seed_epic(home, "e1", status="escalated")
    previous = os.environ.get("MINI_ORK_HOME")
    os.environ["MINI_ORK_HOME"] = str(home)
    try:
        assert epics.main(["retry", "e1"]) == 0
    finally:
        if previous is None:
            os.environ.pop("MINI_ORK_HOME", None)
        else:
            os.environ["MINI_ORK_HOME"] = previous

    con = sqlite3.connect(home / "state.db")
    try:
        status = con.execute("SELECT status FROM epics WHERE id = 'e1'").fetchone()[0]
    finally:
        con.close()
    assert status == "not started"


def test_an_epic_page_opens_no_socket_at_all(home: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """An epic reads ``state.db`` and the kickoff file and nothing else.

    The Board's own rule, kept here: a slow or broken Linear/Jira source must
    not be able to slow or blank an epic's tab.
    """
    import socket

    def _refuse(*_args, **_kwargs):
        raise AssertionError("the task page opened a socket")

    _seed_epic(home, "e1", status="escalated")
    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)

    assert build_page(home, "task", args={"uid": "e1"})["ok"] is True


def test_a_ticket_page_survives_a_transport_that_explodes(home: Path, acme,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """A ticket *does* read its source — and must outlive it failing.

    The page reaches exactly one adapter call, and the adapter is a black box:
    an HTTP client whose socket layer refuses to connect can raise anything at
    all, not just ``ProviderError``. Whatever comes back, the tab renders the
    mirror's row with a callout — never ``{"ok": false}`` and never a traceback.
    """
    import socket

    def _refuse(*_args, **_kwargs):
        raise AssertionError("the task page opened a socket")

    _seed_ticket(home, "acme:ENG-1")
    acme(FakeAcme(raising=AssertionError("the task page opened a socket")))
    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)

    page = build_page(home, "task", args={"uid": "acme:ENG-1"})

    assert page["ok"] is True
    assert _flat_sections(page)[0]["title"] == "Source unreachable"
    assert _section(page, "kv", "Ticket")["items"]


def test_an_epic_and_a_ticket_with_the_same_text_are_different_pages(home: Path) -> None:
    """A uid is resolved as an epic first; the mirror is only the fallback."""
    _seed_epic(home, "acme:ENG-1", status="escalated")
    _seed_ticket(home, "acme:ENG-1")

    page = build_page(home, "task", args={"uid": "acme:ENG-1"})
    assert page["sub"].startswith("Epic · ")   # the epic wins over the mirror row
    assert "Escalated" in [s.get("title") for s in _flat_sections(page)]


def test_now_is_not_baked_into_a_rendered_age(home: Path) -> None:
    """The ages come from ``S.age`` at build time, not from a stored string."""
    _seed_epic(home, "e1", status="escalated")
    before = int(time.time())
    page = build_page(home, "task", args={"uid": "e1"})
    after = int(time.time())

    created = {i["k"]: i for i in _section(page, "kv", "Epic")["items"]}["Created"]["v"]
    assert created.startswith("2026-10-01T00:00:00.000Z")
    assert "ago" in created
    # Built in this test's window, so it cannot have been cached in the row.
    assert after - before < 60


# ── the one page here that reads a source ───────────────────────────────────


def test_safe_error_strips_credentials_but_keeps_the_diagnosis() -> None:
    """What the callout and ``body_error`` are allowed to contain.

    A redactor that eats the diagnosis is worse than the leak it prevents — a
    person cannot fix "token …". So the word has to survive, the *value* must
    not, and it is the shape (an ``=``/``:`` separator, a ``Bearer`` header, a
    ``user:pass@`` authority) that decides which is which.
    """
    from mini_ork.ide_pages import task as task_page

    class Boom(ProviderError):
        pass

    kept = task_page._safe_error(Boom("linear: token expired"))
    assert kept == "Boom: linear: token expired"

    assert "lin_api_secret" not in task_page._safe_error(
        Boom("GET https://api.acme.example/v1?token=lin_api_secret failed"))
    assert "ghp_deadbeef" not in task_page._safe_error(
        Boom("https://x-access-token:ghp_deadbeef@github.com/acme/eng: 401"))
    assert "github.com/acme/eng" in task_page._safe_error(
        Boom("https://x-access-token:ghp_deadbeef@github.com/acme/eng: 401"))
    assert "sk-live-abc" not in task_page._safe_error(
        Boom("rejected: Authorization: Bearer sk-live-abc"))

    # One line, bounded — a traceback never renders and never gets recorded.
    long = task_page._safe_error(Boom("x" * 5000))
    assert "\n" not in long
    assert len(long) <= 301


def test_a_credential_never_reaches_the_page_or_the_mirror(home: Path, acme) -> None:
    """End to end: the token is in the adapter's message, nowhere else."""
    import sqlite3

    _seed_ticket(home, "acme:ENG-1")
    acme(FakeAcme(raising=ProviderError(
        "GET https://api.acme.example/v1/ENG-1?token=lin_api_secret123 failed; "
        "fallback https://x-access-token:ghp_deadbeef@github.com/acme/eng")))

    page = build_page(home, "task", args={"uid": "acme:ENG-1"})

    assert page["ok"] is True
    rendered = " ".join(s.get("text_md", "") for s in _flat_sections(page))
    assert "lin_api_secret123" not in rendered
    assert "ghp_deadbeef" not in rendered
    assert "api.acme.example" in rendered        # the source is still named

    con = sqlite3.connect(home / "state.db")
    try:
        stored = con.execute(
            "SELECT body_error FROM pm_issues WHERE uid = 'acme:ENG-1'").fetchone()[0]
    finally:
        con.close()
    assert stored and "lin_api_secret123" not in stored and "ghp_deadbeef" not in stored
