"""The ticket page's live read — the seam between the mirror and an adapter.

``test_pm_store.py`` pins the mirror's own columns; ``test_ide_pages_task.py``
pins the page's shape. This file is what sits between them: a mirror row, a
source registered under the id that row names, and the page that has to decide —
from the adapter's answer alone — whether to draw a live body or the last cached
one with a callout explaining why.

**Every source here is a fake.** No socket is opened, no token is read, no
endpoint is reached, and that is not a shortcut: the property under test is that
the page survives a source it cannot reach, so a test that reached a real one
would not be testing the property at all.

What is pinned, and why each one is worth a test:

* a read that answers puts its body on the page **and** into the mirror — and
  that cached body is what a later render falls back to when the source dies.
  The two halves are one test on purpose: a cache nothing reads, or a page that
  caches nothing, passes either half alone.
* ``UnknownProvider`` (a ``KeyError``) degrades exactly like ``ProviderError``,
  and neither one is a page *error* — a source that is down is a state the page
  renders, not a fault it reports.
* a source that cannot list comments loses the thread, not the body.
* ``cache_body`` never mints a row: a mirror is written by ``sync``, and a
  half-known issue on the board is worse than an absent one.
* a ``state.db`` whose ``pm_issues`` predates ``0072`` gains the body columns
  on open, which is the only reason the page is safe on an old home.
* the intake pass warms the same cache from the read it was already making.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.ide_pages import build_page  # noqa: E402
from mini_ork.pm import registry, store  # noqa: E402
from mini_ork.pm.model import (  # noqa: E402
    Comment,
    Issue,
    IssueState,
    Priority,
    ProviderIdentity,
)
from mini_ork.pm.provider import Capabilities, ProviderError, Unsupported  # noqa: E402

SOURCE = "acme"
UID = "acme:ENG-1"
BODY = "## Problem\n\nThe widget falls over on start.\n"


def _issue(description: str = BODY, created_at: str = "2026-10-05T09:00:00.000Z") -> Issue:
    return Issue(
        identity=ProviderIdentity(provider=SOURCE, key="ENG-1", project="Project X",
                                  url="https://acme.example/ENG-1"),
        title="Ticket one",
        state=IssueState.IN_PROGRESS,
        native_state="In Progress",
        description=description,
        created_at=created_at,
        updated_at="2026-10-09T00:00:00.000Z",
        priority=Priority.HIGH,
    )


def _comment(author: str, created_at: str) -> Comment:
    return Comment(issue_uid=UID, author=author, body=f"{author} was here",
                   created_at=created_at)


class FakeSource:
    """One source, answering from memory. ``id`` is what the registry keys on."""

    id = SOURCE
    label = "Acme"

    def __init__(self, issue: Issue | None = None, comments=(),
                 *, get_error: BaseException | None = None,
                 comments_error: BaseException | None = None) -> None:
        self.capabilities = Capabilities()
        self._issue = issue
        self._comments = tuple(comments)
        self._get_error = get_error
        self._comments_error = comments_error

    def get(self, uid: str) -> Issue:
        if self._get_error is not None:
            raise self._get_error
        if self._issue is None:
            raise KeyError(uid)
        return self._issue

    def comments(self, uid: str):
        if self._comments_error is not None:
            raise self._comments_error
        return self._comments


@pytest.fixture()
def source():
    """Register a fake under ``acme`` for one test; always take it back out.

    The registry is process-global and outlives the test, so a fake left in
    place would be resolved by every later test that seeds an ``acme:`` row.
    """
    def install(provider: FakeSource) -> FakeSource:
        return registry.register_provider(provider, replace=True)

    yield install
    registry.unregister_provider(SOURCE)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A home whose ``state.db`` has the mirror and nothing else.

    Deliberately not ``migrate.init_db``: the page needs ``pm_issues`` and the
    home directory, and ``store.ensure_schema`` is what actually writes them at
    runtime. Running the whole migration set here would cost seconds per test
    and test the migration runner, which ``test_pm_store.py`` already does.
    """
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    # Seeded with NO description on purpose. ``upsert_issues`` caches the body
    # of any issue that carries one, so a fixture that seeded the full issue
    # would leave half these tests asserting against a cache they did not
    # fill — and the one thing they are all about is what fills it.
    store.upsert_issues(SOURCE, [_issue(description="")], db=str(h / "state.db"))
    return h


def _cache_row(home: Path, uid: str = UID) -> tuple:
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute(
            "SELECT description, created_at, body_synced_at, body_error FROM pm_issues "
            "WHERE uid = ?", (uid,)).fetchone()
    finally:
        con.close()


def _callouts(page: dict) -> list[dict]:
    return [s for s in page["sections"] if s.get("type") == "callout"]


def _section(page: dict, type_: str, title: str | None = None) -> dict:
    for section in page["sections"]:
        if section.get("type") == type_ and (title is None or section.get("title") == title):
            return section
    raise AssertionError(f"no {type_} {title!r} in "
                         f"{[(s.get('type'), s.get('title')) for s in page['sections']]}")


# ── a source that answers ───────────────────────────────────────────────────


def test_a_live_read_is_cached_and_is_what_survives_the_source_dying(home: Path,
                                                                    source) -> None:
    """The whole point of the body cache, in one test and two renders.

    Render once with the source up: the body is drawn and cached. Render again
    with the source down: the *cached* body is drawn, with a callout saying why.
    Either half alone would pass with the feature broken.
    """
    source(FakeSource(_issue(), [_comment("ada", "2026-10-06T10:00:00.000Z")]))

    live = build_page(home, "task", args={"uid": UID})

    assert live["ok"] is True
    assert _callouts(live)[0]["title"] == "Read live"
    assert "The widget falls over on start." in _section(live, "markdown", "Description")["text"]
    assert [i["t"] for i in _section(live, "list", "Comments")["items"]] == ["ada"]

    cached = _cache_row(home)
    assert cached[0] == BODY
    assert cached[1] == "2026-10-05T09:00:00.000Z"
    assert cached[2]                                  # stamped with the read
    assert cached[3] is None                          # and no error to report

    # Now the source goes down — same home, same mirror row, no cache between.
    registry.unregister_provider(SOURCE)
    degraded = build_page(home, "task", args={"uid": UID})

    assert degraded["ok"] is True
    callout = _callouts(degraded)[0]
    assert callout["title"] == "Source unreachable"
    body = _section(degraded, "markdown", "Description (cached)")
    assert body["text"] == BODY                       # the bytes the read cached
    assert "acme:ENG-1" in callout["text_md"]         # which source
    assert cached[2] in callout["text_md"]            # how stale, from the row
    assert "UnknownProvider" in callout["text_md"]    # and why


def test_an_unregistered_source_is_a_state_the_page_renders_not_an_error(home: Path) -> None:
    """``UnknownProvider`` is a ``KeyError``; catching only ``ProviderError``
    would let the commonest misconfiguration blank the tab instead of degrading
    it — and a source being unavailable belongs in a callout, not in the page's
    error banner."""
    page = build_page(home, "task", args={"uid": UID})

    assert page["ok"] is True
    assert not page["errors"], page["errors"]
    assert _callouts(page)[0]["title"] == "Source unreachable"
    assert _section(page, "kv", "Ticket")["items"]


def test_a_source_that_cannot_list_comments_loses_the_thread_not_the_body(
        home: Path, source) -> None:
    """``Unsupported`` is a ``ProviderError``, so the comments catch covers it.

    A body without a thread is a page with less on it; a body replaced by a
    callout because the thread failed would be a page that lied about what it
    could read.
    """
    source(FakeSource(_issue(), comments_error=Unsupported(SOURCE, "comments")))

    page = build_page(home, "task", args={"uid": UID})

    assert page["ok"] is True
    assert _callouts(page)[0]["title"] == "Read live"
    assert "The widget falls over on start." in _section(page, "markdown", "Description")["text"]
    assert "No comments" in _section(page, "list", "Comments")["items"][0]["t"]


def test_a_source_with_an_empty_body_still_renders_a_body_section(home: Path,
                                                                 source) -> None:
    """``''`` is an answer. The mirror stores it; the page says so in words."""
    source(FakeSource(_issue(description="")))

    page = build_page(home, "task", args={"uid": UID})

    assert page["ok"] is True
    assert not [s for s in page["sections"] if s.get("type") == "markdown"]
    assert "no description" in _section(page, "list", "Description")["items"][0]["t"]
    # Cached verbatim — "the source says there is no body" is a fact, and it is
    # exactly what NULL is reserved to *not* mean.
    assert _cache_row(home)[0] == ""


@pytest.mark.parametrize("failure, expected", [
    pytest.param(ProviderError("acme: 503 from the source"), "503", id="provider-error"),
    pytest.param(Unsupported("acme", "get"), "does not support get", id="unsupported"),
    pytest.param(KeyError("acme:ENG-1 is gone"), "is gone", id="keyerror"),
])
def test_every_way_a_read_can_fail_degrades_the_same_way(home: Path, source,
                                                         failure: BaseException,
                                                         expected: str) -> None:
    """One shape for all three, because a caller cannot tell them apart usefully.

    A source that is down (``ProviderError``), one that will not do this at all
    (``Unsupported``, a ``ProviderError`` subclass) and one that no longer has
    the issue (``KeyError``, which ``UnknownProvider`` also is) all mean the
    same thing to a reader: no live body, here is the cache. Only the reason in
    the callout differs, and it is named so the person who owns the source can
    act on it.
    """
    source(FakeSource(get_error=failure))

    page = build_page(home, "task", args={"uid": UID})

    assert page["ok"] is True
    assert not page["errors"], page["errors"]
    callout = _callouts(page)[0]
    assert callout["title"] == "Source unreachable"
    assert expected in callout["text_md"]


# ── the mirror's own rules ──────────────────────────────────────────────────


def test_cache_body_never_mints_a_row(home: Path) -> None:
    """A uid with no mirror row is not a ticket the page may invent.

    ``cache_body`` is an ``UPDATE`` and not an upsert for exactly this reason:
    the mirror is written by ``sync``, and a row minted from a page render would
    be a half-known issue on the board with no title or state behind it.
    """
    db = str(home / "state.db")
    never_mirrored = _issue().identity
    unmirrored = Issue(identity=ProviderIdentity(provider=SOURCE, key="ENG-9"),
                       title="", state=IssueState.TODO, native_state="Todo")
    assert never_mirrored.key == "ENG-1"              # the seeded row, for contrast
    assert store.row(UID, db=db) is not None

    assert store.cache_body(unmirrored.uid, description="a body", created_at=None,
                            db=db) is False
    assert store.row("acme:ENG-9", db=db) is None
    assert len(store.query_rows(db=db)) == 1          # and no row was minted


def test_record_body_error_keeps_the_body_and_its_age(home: Path) -> None:
    """The fallback and its timestamp are the two things a failure must not eat."""
    db = str(home / "state.db")
    store.cache_body(UID, description=BODY, created_at="2026-10-05T09:00:00.000Z",
                     body_synced_at="2026-10-09T00:00:00.000Z", db=db)

    assert store.record_body_error(UID, "acme: unreachable", db) is True

    row = store.row(UID, db=db)
    assert row.description == BODY                    # untouched
    assert row.body_synced_at == "2026-10-09T00:00:00.000Z"
    assert row.body_error == "acme: unreachable"
    assert row.created_at == "2026-10-05T09:00:00.000Z"


def test_a_mirror_that_predates_0072_gains_the_columns_on_open(tmp_path: Path) -> None:
    """The runtime half of the migration: an old home must still page.

    ``CREATE TABLE IF NOT EXISTS`` cannot widen a table that already exists, so
    without ``_ensure_body_columns`` the very first ``SELECT description`` on a
    ``state.db`` written before 0072 raises — and the ticket tab 500s on a home
    that worked yesterday.
    """
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE pm_issues (uid TEXT PRIMARY KEY, provider TEXT NOT NULL,"
            " native_key TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,"
            " native_state TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,"
            " native_priority INTEGER, url TEXT, assignee TEXT, labels TEXT,"
            " parent_uid TEXT, project TEXT, updated_at TEXT, synced_at TEXT NOT NULL,"
            " UNIQUE (provider, native_key))")
        con.execute(
            "INSERT INTO pm_issues (uid, provider, native_key, title, state, native_state,"
            " priority, synced_at) VALUES (?,?,?,?,?,?,?,?)",
            (UID, SOURCE, "ENG-1", "Ticket one", "todo", "Todo", 0,
             "2026-10-10T00:00:00.000Z"))
        con.commit()
    finally:
        con.close()

    row = store.row(UID, db=db)                       # opens, widens, reads

    assert row is not None
    assert row.title == "Ticket one"
    assert (row.description, row.created_at, row.body_synced_at, row.body_error) == (
        None, None, None, None)                       # never read live: a state, not a fault
    assert store.cache_body(UID, description=BODY, created_at=None, db=db) is True


def test_the_intake_pass_warms_the_cache_from_the_read_it_already_makes(home: Path) -> None:
    """The daemon's one free full read is the one that fills the cache.

    ``poll_once`` pays for ``provider.get`` per candidate anyway, so a ticket
    that intake has looked at is a ticket whose body the page will not have to
    fetch — which is the difference between a board that opens instantly and
    one that waits on a source per ticket.
    """
    from mini_ork.intake import poll

    db = str(home / "state.db")
    assert store.row(UID, db=db).description is None

    poll._warm_body_cache(_issue(), db)

    row = store.row(UID, db=db)
    assert row.description == BODY
    assert row.created_at == "2026-10-05T09:00:00.000Z"
    assert row.body_synced_at and row.body_error is None


def test_a_failing_cache_write_cannot_fail_the_pass(tmp_path: Path) -> None:
    """Best-effort by construction: the cache is an optimization, never a
    dependency, so a mirror it cannot write must not stop a dispatch."""
    from mini_ork.intake import poll

    missing = str(tmp_path / "no-such-dir" / "state.db")

    poll._warm_body_cache(_issue(), missing)          # must not raise
