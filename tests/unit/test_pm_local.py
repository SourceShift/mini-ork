"""Hermetic tests for ``mini_ork.pm.providers.local`` — mini-ork's own epics.

The fixture builds a real state.db from ``db/migrations/0001_core.sql`` rather
than a hand-written epics table, so a column that disappears from the schema
breaks this test instead of passing against a guess.

What is pinned, in the order the adapter matters:

* status mapping, including the two places it is deliberately unequal —
  ``escalated`` reads as BLOCKED, an archived epic reads as CANCELED, and a
  status from a newer engine reads as BACKLOG with ``native_state`` intact
* priority, which counts the opposite way from Linear
* the refusals: no transition, no comments, no cross-source links, and no
  silent drop of an assignee or label write
* creating an epic mints a unique slug, the way ``epics ingest`` does
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.pm.model import IssueState, LinkKind, Priority  # noqa: E402
from mini_ork.pm.provider import Draft, Patch, Query, Unsupported  # noqa: E402
from mini_ork.pm.providers import local as local_module  # noqa: E402

MIGRATIONS = ("0001_core.sql", "0027_epic_dependencies.sql")


@pytest.fixture
def db(tmp_path: Path) -> str:
    import sqlite3

    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    try:
        for name in MIGRATIONS:
            con.executescript((REPO / "db" / "migrations" / name).read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()
    return str(path)


@pytest.fixture
def source(db: str) -> local_module.LocalProvider:
    # Priority is a runtime add rather than part of 0001_core.sql, so the
    # adapter's own ensure call is what the tests exercise.
    from mini_ork.scheduler import ensure_priority_column

    ensure_priority_column(db)
    return local_module.LocalProvider(db)


def _epic(db: str, epic_id: str, title: str, status: str = "not started", **columns) -> None:
    import sqlite3

    con = sqlite3.connect(db)
    try:
        names = ["id", "title", "status", *columns]
        values = [epic_id, title, status, *columns.values()]
        con.execute(
            f"INSERT INTO epics({', '.join(names)}) VALUES ({','.join('?' * len(names))})",
            values,
        )
        con.commit()
    finally:
        con.close()


def _titles(page) -> list[str]:
    return [issue.title for issue in page.issues]


def test_lists_open_work_newest_first(source, db):
    _epic(db, "a", "Older", updated_at="2026-10-01T00:00:00.000Z")
    _epic(db, "b", "Newer", updated_at="2026-10-09T00:00:00.000Z")
    assert _titles(source.list_issues(Query())) == ["Newer", "Older"]


def test_status_maps_onto_the_normalized_vocabulary(source, db):
    _epic(db, "a", "Working", "in progress")
    _epic(db, "b", "Waiting", "blocked")
    _epic(db, "c", "Shipped", "done")
    states = {
        issue.title: (issue.state, issue.native_state)
        for issue in source.list_issues(Query()).issues
    }
    assert states["Working"] == (IssueState.IN_PROGRESS, "in progress")
    assert states["Waiting"] == (IssueState.BLOCKED, "blocked")
    assert states["Shipped"] == (IssueState.DONE, "done")


def test_a_held_epic_reads_as_blocked_not_as_its_own_lane(source, db):
    # `escalated` and `blocked` are two words for "a person has to look at
    # this"; the native word survives so the card can still say which.
    _epic(db, "a", "Escalated", "escalated")
    issue = source.list_issues(Query()).issues[0]
    assert issue.state is IssueState.BLOCKED
    assert issue.native_state == "escalated"


def test_an_unknown_status_lands_in_backlog_with_its_word_kept(tmp_path):
    """The fallback guards a status added by a NEWER engine's schema.

    ``0001_core.sql``'s CHECK constraint forbids such a value outright, so this
    has to be built on a hand-made table — which is the point: the case only
    arises when the schema itself has moved on.
    """
    import sqlite3

    path = str(tmp_path / "future.db")
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE epics (id TEXT PRIMARY KEY, title TEXT, status TEXT)")
        con.execute("INSERT INTO epics VALUES ('a', 'From a newer engine', 'triaging')")
        con.commit()
    finally:
        con.close()

    issue = local_module.LocalProvider(path).list_issues(Query()).issues[0]
    assert issue.state is IssueState.BACKLOG
    assert issue.native_state == "triaging"


def test_an_archived_epic_reads_as_canceled(source, db):
    _epic(db, "a", "Archived", archived_at="2026-10-02T00:00:00.000Z")
    _epic(db, "b", "Live")
    assert _titles(source.list_issues(Query())) == ["Live"]
    closed = source.list_issues(Query(states=(IssueState.CANCELED,)))
    assert _titles(closed) == ["Archived"]
    assert closed.issues[0].state is IssueState.CANCELED


def test_a_state_with_no_native_spelling_matches_nothing(source, db):
    # BACKLOG has no column in mini-ork; answering with the whole table would
    # look like the filter had been applied.
    _epic(db, "a", "Something")
    assert source.list_issues(Query(states=(IssueState.BACKLOG,))).issues == ()


def test_filtering_by_state_unions_the_words_that_share_it(source, db):
    _epic(db, "a", "Held", "blocked")
    _epic(db, "b", "Escalated", "escalated")
    _epic(db, "c", "Working", "in progress")
    blocked = source.list_issues(Query(states=(IssueState.BLOCKED,)))
    assert sorted(_titles(blocked)) == ["Escalated", "Held"]


def test_priority_counts_the_opposite_way_from_linear(source, db):
    _epic(db, "a", "Important", priority=3)
    _epic(db, "b", "Later", priority=1)
    issues = {issue.title: issue for issue in source.list_issues(Query()).issues}
    assert issues["Important"].priority is Priority.HIGH
    assert issues["Important"].native_priority == 3
    assert issues["Later"].priority is Priority.LOW


def test_text_search_covers_title_and_id(source, db):
    _epic(db, "retry-budget", "Give the retry loop a budget")
    _epic(db, "other", "Unrelated")
    assert _titles(source.list_issues(Query(text="retry"))) == ["Give the retry loop a budget"]
    assert _titles(source.list_issues(Query(text="retry-budget"))) == [
        "Give the retry loop a budget"
    ]


def test_labels_come_from_the_epic_kind(source, db):
    _epic(db, "a", "Backend work", epic_kind="be")
    assert source.list_issues(Query()).issues[0].labels == ("be",)


def test_runtime_columns_reach_native_but_never_the_assignee(source, db):
    from mini_ork.scheduler import ensure_retry_schema

    ensure_retry_schema(db)
    _epic(db, "a", "Recipe work", recipe="eng-bug", worker_default="implementer", attempts=2)
    issue = source.list_issues(Query()).issues[0]
    assert issue.native["recipe"] == "eng-bug"
    assert issue.native["worker_default"] == "implementer"
    assert issue.native["attempts"] == 2
    # mini-ork assigns work to a lane, not a person, which is why `assignees`
    # is false and this stays empty even though a worker is named.
    assert issue.assignee is None


def test_get_by_uid_and_the_two_ways_it_fails(source, db):
    _epic(db, "a", "Something")
    assert source.get("local:a").title == "Something"
    with pytest.raises(KeyError):
        source.get("local:missing")
    with pytest.raises(KeyError):
        source.get("linear:ENG-1")


def test_creating_an_epic_mints_a_slug_the_way_ingest_does(source, db):
    created = source.create(Draft(title="Add a retry budget", priority=Priority.HIGH))
    assert created.identity.key == "add-a-retry-budget"
    assert created.state is IssueState.TODO
    assert created.native_state == "not started"
    assert created.priority is Priority.HIGH

    second = source.create(Draft(title="Add a retry budget"))
    assert second.identity.key == "add-a-retry-budget-2"


def test_create_carries_the_description_and_the_kind(source, db):
    created = source.create(
        Draft(title="Something", description="Why it matters", labels=("be",))
    )
    assert created.description == "Why it matters"
    assert created.labels == ("be",)


def test_create_refuses_an_empty_title_and_an_unknown_kind(source):
    from mini_ork.pm.provider import ProviderError

    with pytest.raises(ProviderError):
        source.create(Draft(title="   "))
    with pytest.raises(ProviderError):
        source.create(Draft(title="Something", labels=("nonsense",)))


def test_update_changes_title_description_and_priority(source, db):
    _epic(db, "a", "Before")
    updated = source.update(
        "local:a", Patch(title="After", description="Notes", priority=Priority.URGENT)
    )
    assert updated.title == "After"
    assert updated.description == "Notes"
    assert updated.priority is Priority.URGENT


def test_update_with_nothing_to_do_returns_the_issue(source, db):
    _epic(db, "a", "Unchanged")
    assert source.update("local:a", Patch()).title == "Unchanged"


def test_update_refuses_an_assignee_rather_than_dropping_it(source, db):
    _epic(db, "a", "Something")
    with pytest.raises(Unsupported):
        source.update("local:a", Patch(assignee="someone"))


def test_status_is_not_writable_from_here(source, db):
    _epic(db, "a", "Something")
    with pytest.raises(Unsupported):
        source.transition("local:a", IssueState.DONE)


def test_there_are_no_comments(source, db):
    _epic(db, "a", "Something")
    with pytest.raises(Unsupported):
        source.comments("local:a")
    with pytest.raises(Unsupported):
        source.comment("local:a", "a note")


def test_blocks_becomes_a_hard_dependency_and_relates_a_soft_one(source, db):
    import sqlite3

    _epic(db, "a", "First")
    _epic(db, "b", "Second")
    source.link("local:a", "local:b", LinkKind.BLOCKS)
    source.link("local:b", "local:a", LinkKind.RELATES)

    con = sqlite3.connect(db)
    try:
        edges = {
            (row[0], row[1]): row[2]
            for row in con.execute(
                "SELECT from_epic_id, to_epic_id, kind FROM epic_dependencies"
            )
        }
    finally:
        con.close()
    assert edges[("a", "b")] == "hard"
    assert edges[("b", "a")] == "soft"


def test_a_link_kind_with_no_column_is_refused(source, db):
    _epic(db, "a", "First")
    _epic(db, "b", "Second")
    with pytest.raises(Unsupported):
        source.link("local:a", "local:b", LinkKind.DUPLICATES)


def test_capabilities_say_what_the_adapter_cannot_do(source):
    capabilities = local_module.LocalProvider.capabilities
    assert capabilities.create is True
    assert capabilities.update is True
    assert capabilities.transition is False
    assert capabilities.comment is False
    assert capabilities.assignees is False
    assert capabilities.read_only is False
