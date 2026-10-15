"""Hermetic tests for ``mini_ork.pm.store`` — the local mirror.

Everything here runs against a throwaway state.db in ``tmp_path``. What is
pinned:

* ``ensure_schema`` is idempotent, so it can run on every open
* the schema it writes is the schema ``0069_pm_mirror.sql`` writes — the pair
  cannot drift apart quietly, which is the only reason it is safe to have two
* an upsert is an update, not a duplicate, and a missing column in an old row
  is not a crash
* the mirror survives a source failure: the rows stay and the failure is noted
* links are idempotent and readable from both ends

It also owns 0072, the body cache, because that is still this table: the
columns are nullable with no default (NULL is "never read live", a state, not a
fault), a leaner later sync never blanks a body an earlier one cached, and
``cache_body``/``record_body_error`` move only what they name. The one rule
that matters most here is the second of those — a regression in it silently
empties every ticket page on the next poll, and nothing else would notice.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.pm import store  # noqa: E402
from mini_ork.pm.model import (  # noqa: E402
    Issue,
    IssueState,
    LinkKind,
    Priority,
    ProviderIdentity,
)
from mini_ork.pm.provider import Page, ProviderError, Query  # noqa: E402

MIGRATION = REPO / "db" / "migrations" / "0069_pm_mirror.sql"
BODY_MIGRATION = REPO / "db" / "migrations" / "0072_pm_mirror_body.sql"


def _issue(key: str, title: str = "Do the thing", provider: str = "local", **overrides) -> Issue:
    fields = {
        "identity": ProviderIdentity(provider=provider, key=key),
        "title": title,
        "state": IssueState.TODO,
        "native_state": "not started",
        "priority": Priority.MEDIUM,
    }
    fields.update(overrides)
    return Issue(**fields)


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "state.db")


def test_schema_is_idempotent_and_the_only_tables_it_makes(db: str):
    store.ensure_schema(db)
    store.ensure_schema(db)
    import sqlite3

    con = sqlite3.connect(db)
    try:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert {"pm_issues", "pm_links", "pm_sync"} <= names


def test_schema_matches_the_migration():
    """The runtime DDL and the migration must be the same text.

    Two writers for one schema is only safe while they agree; this is the test
    that keeps them agreeing.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    # SQL line comments and whitespace differ between a migration and a DDL
    # constant by construction; the statements must not.
    without_comments = re.sub(r"--[^\n]*", " ", text)
    normalized = re.sub(r"\s+", " ", without_comments).strip()
    for statement in store.SCHEMA_SQL:
        assert re.sub(r"\s+", " ", statement).strip() in normalized, statement


def test_upsert_then_read_back(db: str):
    written = store.upsert_issues(
        "local",
        [
            _issue(
                "add-retry-budget",
                "Add a retry budget",
                labels=("be",),
                assignee=None,
                updated_at="2026-10-10T10:00:00.000Z",
            )
        ],
        db,
    )
    assert written == 1
    rows = store.query_rows(db=db)
    assert [row.uid for row in rows] == ["local:add-retry-budget"]
    row = rows[0]
    assert row.title == "Add a retry budget"
    assert row.state is IssueState.TODO
    assert row.native_state == "not started"
    assert row.priority is Priority.MEDIUM
    assert row.labels == ("be",)


def test_upsert_updates_rather_than_duplicates(db: str):
    store.upsert_issues("local", [_issue("e1", "First")], db)
    store.upsert_issues("local", [_issue("e1", "Second")], db)
    rows = store.query_rows(db=db)
    assert len(rows) == 1
    assert rows[0].title == "Second"


def test_filters_are_anded(db: str):
    store.upsert_issues(
        "local",
        [
            _issue("e1", "Retry budget", state=IssueState.IN_PROGRESS, native_state="in progress"),
            _issue("e2", "Retry budget notes", state=IssueState.DONE, native_state="done"),
            _issue("e3", "Something else", state=IssueState.IN_PROGRESS, native_state="in progress"),
        ],
        db,
    )
    in_progress = store.query_rows(states=(IssueState.IN_PROGRESS,), db=db)
    assert {row.uid for row in in_progress} == {"local:e1", "local:e3"}

    both = store.query_rows(states=(IssueState.IN_PROGRESS,), text="Retry", db=db)
    assert {row.uid for row in both} == {"local:e1"}


def test_text_search_hits_the_key_as_well_as_the_title(db: str):
    store.upsert_issues("local", [_issue("ENG-12", "Nothing to do with the key")], db)
    assert [row.uid for row in store.query_rows(text="eng-12", db=db)] == ["local:ENG-12"]


def test_a_row_written_by_a_newer_version_reads_as_backlog(db: str):
    """One unreadable row must not take the whole board down."""
    import sqlite3

    store.ensure_schema(db)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO pm_issues (uid, provider, native_key, title, state, native_state,"
            " priority, synced_at) VALUES ('local:e9','local','e9','Future','working_on_it',"
            "'working_on_it', 0, '2026-10-10T00:00:00.000Z')"
        )
        con.commit()
    finally:
        con.close()
    rows = store.query_rows(db=db)
    assert rows[0].state is IssueState.BACKLOG
    assert rows[0].native_state == "working_on_it"


def test_mirror_state_reports_the_sync(db: str):
    store.upsert_issues("local", [_issue("e1")], db, synced_at="2026-10-10T09:00:00.000Z")
    state = store.mirror_state(db=db)
    assert state == [
        {"provider": "local", "synced_at": "2026-10-10T09:00:00.000Z", "n": 1, "error": None}
    ]


class FailingSource:
    id = "linear"
    label = "Failing"

    def list_issues(self, query: Query) -> Page:
        raise ProviderError("linear: token expired")


def test_a_failed_sync_keeps_the_rows_and_notes_the_reason(db: str):
    store.upsert_issues("local", [_issue("e1")], db)
    with pytest.raises(ProviderError):
        store.sync(FailingSource(), db=db)
    # The board still has what it knew.
    assert len(store.query_rows(db=db)) == 1
    recorded = {row["provider"]: row for row in store.mirror_state(db=db)}
    assert recorded["linear"]["error"] == "linear: token expired"


class StubSource:
    id = "stub"
    label = "Stub"

    def __init__(self, issues):
        self._issues = issues

    def list_issues(self, query: Query) -> Page:
        return Page(issues=tuple(self._issues))


def test_sync_mirrors_a_page(db: str):
    written = store.sync(
        StubSource([_issue("e1", provider="stub"), _issue("e2", provider="stub")]), db=db
    )
    assert written == 2
    assert {row.uid for row in store.query_rows(db=db)} == {"stub:e1", "stub:e2"}


def test_an_issue_from_another_source_is_refused(db: str):
    # The uid would say 'linear' and the row's provider column 'local' — a
    # mirror whose halves disagree with each other.
    with pytest.raises(ValueError):
        store.upsert_issues("local", [_issue("ENG-1", provider="linear")], db)


def test_links_are_idempotent_and_readable_from_both_ends(db: str):
    first = store.link("local:e1", "run:abc123", LinkKind.RUN, db=db)
    again = store.link("local:e1", "run:abc123", LinkKind.RUN, db=db)
    assert first.created_at == again.created_at

    from_the_issue = store.links_for("local:e1", db=db)
    from_the_run = store.links_for("run:abc123", db=db)
    assert len(from_the_issue) == 1
    assert from_the_run == from_the_issue
    assert from_the_run[0].kind is LinkKind.RUN
    assert from_the_run[0].to_uid == "run:abc123"


def test_link_accepts_the_plain_string_kind(db: str):
    # A page payload carries the kind as a string; LinkKind hashes by NAME, so
    # a naive lookup would miss. The store resolves it instead.
    linked = store.link("local:e1", "local:e2", "blocks", db=db)
    assert linked.kind is LinkKind.BLOCKS


# ── 0072: the body cache ────────────────────────────────────────────────────


def test_the_body_columns_are_the_ones_0072_adds():
    """Two writers, one schema — the pair that cannot be allowed to drift.

    The runtime ``_ensure_body_columns`` and the migration must add the same
    columns, or a fresh home and an upgraded one disagree about the table the
    ticket page reads.
    """
    text = re.sub(r"\s+", " ", BODY_MIGRATION.read_text(encoding="utf-8"))
    for name, decl in store.BODY_COLUMNS:
        assert f"ADD COLUMN {name} {decl};" in text, name
    # One guarded block, gated on the first column: they are added together or
    # not at all, which is what makes the reapply a clean no-op.
    assert "pragma_table_info" in text
    assert text.count("ALTER TABLE pm_issues ADD COLUMN") == len(store.BODY_COLUMNS)


def test_the_body_columns_are_nullable_with_no_default(db: str):
    """NULL is "never read live" — a state the page must render, not a fault.

    A ``DEFAULT ''`` would collapse it into "the source answered and the body is
    empty", and an unreachable never-cached ticket would read exactly like an
    empty one. This is the assertion that keeps the two apart.
    """
    import sqlite3

    store.ensure_schema(db)
    con = sqlite3.connect(db)
    try:
        columns = {r[1]: r for r in con.execute("PRAGMA table_info(pm_issues)")}
    finally:
        con.close()
    for name, decl in store.BODY_COLUMNS:
        column = columns[name]      # cid, name, type, notnull, dflt_value, pk
        assert column[2] == decl, name
        assert column[3] == 0, f"{name} must be nullable"
        assert column[4] is None, f"{name} must have no default"


def test_a_later_upsert_never_blanks_a_cached_body(db: str):
    """The subtlest edit in the mirror, and the one with the loudest failure.

    ``Issue`` spells "no description" as ``''``, so a leaner later sync — a page
    that asked for no description, a source that stopped returning one — would
    overwrite a cached body with an empty string, and every ticket page would
    quietly go blank on the next poll.
    """
    store.upsert_issues(
        "local", [_issue("e1", "Do the thing", description="the body text")], db)
    assert store.row("local:e1", db=db).description == "the body text"

    store.upsert_issues("local", [_issue("e1", "Do the thing, renamed")], db)

    row = store.row("local:e1", db=db)
    assert row.title == "Do the thing, renamed"      # the sync did update
    assert row.description == "the body text"        # and did not blank
    assert row.created_at is None or row.created_at == "", row.created_at


def test_a_batch_that_carries_a_body_refreshes_the_cache(db: str):
    """The other half: the sync path may warm the cache, not only preserve it."""
    store.upsert_issues("local", [_issue("e1", description="first")], db)
    store.upsert_issues(
        "local",
        [_issue("e1", description="second", created_at="2026-10-05T09:00:00.000Z")],
        db,
    )
    row = store.row("local:e1", db=db)
    assert row.description == "second"
    assert row.created_at == "2026-10-05T09:00:00.000Z"


def test_cache_body_stamps_the_read_and_clears_a_previous_error(db: str):
    store.upsert_issues("local", [_issue("e1")], db)
    assert store.record_body_error("local:e1", "local: unreachable", db) is True
    assert store.row("local:e1", db=db).body_error == "local: unreachable"

    assert store.cache_body(
        "local:e1",
        description="the body",
        created_at="2026-10-05T09:00:00.000Z",
        body_synced_at="2026-10-11T12:00:00.000Z",
        db=db,
    ) is True

    row = store.row("local:e1", db=db)
    assert row.description == "the body"
    assert row.created_at == "2026-10-05T09:00:00.000Z"
    assert row.body_synced_at == "2026-10-11T12:00:00.000Z"
    # The source answered, so the reason it failed before is no longer true.
    assert row.body_error is None


def test_cache_body_stamps_itself_when_the_caller_does_not(db: str):
    """An empty ``body_synced_at`` means "now" — one clock, inside the store.

    A caller with its own instant (a batch sync) passes it and gets it back; a
    caller that just read one ticket does not have to reach for a clock of its
    own, which is how a page comes to stamp a read with a time it guessed.
    """
    store.upsert_issues("local", [_issue("e1")], db)
    before = store._now()
    store.cache_body("local:e1", description="b", created_at=None, db=db)
    after = store._now()
    stamp = store.row("local:e1", db=db).body_synced_at
    assert before <= stamp <= after


def test_cache_body_keeps_an_empty_description_verbatim(db: str):
    """After a read, ``''`` is a fact. NULL is what "not read" is for."""
    store.upsert_issues("local", [_issue("e1")], db)
    store.cache_body("local:e1", description="", created_at=None, db=db)
    assert store.row("local:e1", db=db).description == ""


def test_a_body_larger_than_the_column_is_not_truncated_by_the_store(db: str):
    """The store records what it was given; the *page* owns the display cap.

    Asserted because the obvious "fix" — clipping on write — would destroy the
    only copy, and a page that renders 200k of a 400k body must not have cost
    the mirror the other 200k.
    """
    big = "x" * 500_000
    store.upsert_issues("local", [_issue("e1")], db)
    store.cache_body("local:e1", description=big, created_at=None, db=db)
    assert len(store.row("local:e1", db=db).description) == len(big)
