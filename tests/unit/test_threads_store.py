"""Hermetic tests for ``mini_ork.threads.store`` — the thread transcript.

Everything runs against a throwaway state.db in ``tmp_path``. What is pinned:

* ``ensure_schema`` is idempotent, so it can run on every open
* the schema it writes is the schema ``0070_threads.sql`` writes — the pair
  cannot drift apart quietly, which is the only reason it is safe to have two
* a turn is allocated once and never reused, so two follow-ups cannot collide
* ``event_id`` and ``external_id`` make a duplicate a no-op, not a second row
* a run is linked once per attempt, and its terminal state re-derives the
  thread's status
* an intake event replays as one row, not two
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.threads import store  # noqa: E402
from mini_ork.threads.model import (  # noqa: E402
    MessageRole,
    RunStatus,
    ThreadOrigin,
    ThreadState,
)

MIGRATION = REPO / "db" / "migrations" / "0070_threads.sql"


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
    assert {"threads", "thread_messages", "thread_runs", "intake_events"} <= names


def test_schema_matches_the_migration():
    """The runtime DDL and the migration must be the same text.

    Two writers for one schema is only safe while they agree; this is the test
    that keeps them agreeing.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    without_comments = re.sub(r"--[^\n]*", " ", text)
    normalized = re.sub(r"\s+", " ", without_comments).strip()
    for statement in store.SCHEMA_SQL:
        assert re.sub(r"\s+", " ", statement).strip() in normalized, statement


def test_open_thread_is_idempotent(db: str):
    first = store.open_thread("t1", title="Fix the thing", origin=ThreadOrigin.INTAKE,
                              origin_ref="github:o/r#1", db=db)
    store.append_message("t1", role=MessageRole.USER, text="hello", db=db)
    again = store.open_thread("t1", title="A different title", db=db)

    # A second open does not reset the row — the turn we spent is still spent.
    assert again.id == first.id
    assert again.title == "Fix the thing"
    assert again.last_turn == 1
    assert again.status is ThreadState.IDLE


def test_a_turn_is_allocated_once_and_never_reused(db: str):
    store.open_thread("t1", db=db)
    opened = store.append_message("t1", role=MessageRole.USER, text="first", db=db)
    answered = store.append_message("t1", role=MessageRole.ASSISTANT, text="…",
                                    turn=opened.turn, event_id="ev-1", db=db)
    second = store.append_message("t1", role=MessageRole.USER, text="second", db=db)

    assert opened.turn == 1
    assert answered.turn == 1          # the run's output belongs to round 1
    assert second.turn == 2            # the next user message opens round 2
    assert store.get_thread("t1", db=db).last_turn == 2

    transcript = store.messages("t1", db=db)
    assert [m.text for m in transcript] == ["first", "…", "second"]
    assert [m.turn for m in transcript] == [1, 1, 2]


def test_an_explicit_turn_still_advances_the_allocator(db: str):
    """The mirror path writes attempt 1's turn 1 explicitly. If that left
    last_turn at 0 the follow-up would be handed turn 1 again — two rounds
    under one number, and the continuation's run named after the wrong turn."""
    store.open_thread("t1", db=db)
    store.append_message("t1", role=MessageRole.USER, text="mirrored", turn=1, db=db)

    assert store.get_thread("t1", db=db).last_turn == 1
    follow_up = store.append_message("t1", role=MessageRole.USER, text="next", db=db)
    assert follow_up.turn == 2


def test_a_projected_output_does_not_pull_the_allocator_backwards(db: str):
    """A run's output can attach to an earlier turn than the newest; the
    allocator must keep the maximum, not take the last write."""
    store.open_thread("t1", db=db)
    store.append_message("t1", role=MessageRole.USER, text="one", db=db)      # turn 1
    store.append_message("t1", role=MessageRole.USER, text="two", db=db)      # turn 2
    store.append_message("t1", role=MessageRole.ASSISTANT, text="…",
                         turn=1, event_id="late", db=db)

    assert store.get_thread("t1", db=db).last_turn == 2


def test_a_repeated_event_id_is_the_same_row(db: str):
    store.open_thread("t1", db=db)
    turn = store.append_message("t1", role=MessageRole.USER, text="go", db=db).turn
    first = store.append_message("t1", role=MessageRole.ASSISTANT, text="working",
                                 turn=turn, event_id="ev-1", db=db)
    # observe() runs on every poll tick; the same event must not append twice.
    again = store.append_message("t1", role=MessageRole.ASSISTANT, text="working",
                                 turn=turn, event_id="ev-1", db=db)
    assert again.id == first.id
    assert len(store.messages("t1", db=db)) == 2


def test_a_repeated_external_id_is_the_same_row(db: str):
    store.open_thread("t1", db=db)
    first = store.append_message("t1", role=MessageRole.USER, text="hi",
                                 external_id="slack:C1/123.45", db=db)
    # A Slack redelivery of the same message is not a second turn.
    again = store.append_message("t1", role=MessageRole.USER, text="hi",
                                 external_id="slack:C1/123.45", db=db)
    assert again.id == first.id
    assert store.get_thread("t1", db=db).last_turn == 1
    assert store.message_by_external("t1", "slack:C1/123.45", db=db).id == first.id


def test_messages_limit_keeps_the_last_n(db: str):
    store.open_thread("t1", db=db)
    for i in range(5):
        store.append_message("t1", role=MessageRole.USER, text=f"m{i}", db=db)
    tail = store.messages("t1", limit=2, db=db)
    assert [m.text for m in tail] == ["m3", "m4"]


def test_link_run_is_one_row_per_attempt_and_found_live(db: str):
    store.open_thread("t1", db=db)
    first = store.link_run("t1", "t1", attempt=1, recipe="code-fix", db=db)
    # A second dispatch of attempt 1 is a no-op — the double-dispatch guard.
    again = store.link_run("t1", "t1", attempt=1, db=db)
    assert again.id == first.id

    live = store.live_run("t1", db=db)
    assert live is not None and live.run_id == "t1" and live.status is RunStatus.RUNNING
    assert store.get_thread("t1", db=db).status is ThreadState.RUNNING


def test_settling_a_run_re_derives_the_thread_status(db: str):
    store.open_thread("t1", db=db)
    store.link_run("t1", "t1", attempt=1, db=db)
    thread = store.settle_run("t1", RunStatus.FINISHED, db=db)
    assert thread.status is ThreadState.FINISHED
    assert store.live_run("t1", db=db) is None

    # A second completion signal must not flip a finished thread back to running.
    assert store.settle_run("t1", RunStatus.ERROR, db=db) is None
    assert store.get_thread("t1", db=db).status is ThreadState.FINISHED


def test_a_continuation_is_attempt_two_in_the_same_thread(db: str):
    store.open_thread("t1", db=db)
    store.link_run("t1", "t1", attempt=1, db=db)
    store.settle_run("t1", RunStatus.FINISHED, db=db)

    store.link_run("t1", "t1-t2", attempt=2, kickoff_path="runs-inbox/t1-t2.md", db=db)
    runs = store.runs_for("t1", db=db)
    assert [r.attempt for r in runs] == [1, 2]
    assert runs[1].run_id == "t1-t2"
    assert store.get_thread("t1", db=db).status is ThreadState.RUNNING
    assert store.live_run("t1", db=db).attempt == 2


def test_an_error_run_settles_the_thread_to_error(db: str):
    store.open_thread("t1", db=db)
    store.link_run("t1", "t1", attempt=1, db=db)
    assert store.settle_run("t1", RunStatus.ERROR, db=db).status is ThreadState.ERROR


def test_recompute_status_rebuilds_a_stale_cache(db: str):
    store.open_thread("t1", db=db)
    store.link_run("t1", "t1", attempt=1, db=db)
    store.settle_run("t1", RunStatus.FINISHED, db=db)
    # Corrupt the cache the way a crash mid-write would.
    import sqlite3

    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE threads SET status = 'running' WHERE id = 't1'")
        con.commit()
    finally:
        con.close()
    assert store.get_thread("t1", db=db).status is ThreadState.RUNNING
    assert store.recompute_status("t1", db=db).status is ThreadState.FINISHED


def test_find_thread_returns_the_newest_for_a_provider_key(db: str):
    assert store.find_thread(ThreadOrigin.INTAKE, "github:o/r#1", db=db) is None
    store.open_thread("a", origin=ThreadOrigin.INTAKE, origin_ref="github:o/r#1", db=db)
    store.open_thread("b", origin=ThreadOrigin.INTAKE, origin_ref="github:o/r#1", db=db)
    assert store.find_thread(ThreadOrigin.INTAKE, "github:o/r#1", db=db).id == "b"
    assert store.find_thread(ThreadOrigin.INTAKE, "github:o/r#2", db=db) is None


def test_intake_event_replays_as_one_row(db: str):
    store.record_intake_event("github", "delivery-1", "hash-a",
                              issue_uid="github:o/r#1", thread_id="t1",
                              action="launched", db=db)
    again = store.record_intake_event("github", "delivery-1", "hash-a",
                                      issue_uid="github:o/r#1", thread_id="t1",
                                      action="launched", db=db)
    assert again.delivery_id == "delivery-1"
    assert store.intake_event("github", "delivery-1", db=db) is not None
    assert store.intake_event("github", "delivery-2", db=db) is None
    assert len(store.intake_events_for("github:o/r#1", db=db)) == 1


def test_an_unknown_origin_or_status_reads_as_the_safe_default(db: str):
    """One row written by a newer version must not take a whole sidebar down."""
    import sqlite3

    store.ensure_schema(db)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO threads (id, title, status, origin, last_turn, created_at, updated_at)"
            " VALUES ('t9','future','paused','telepathy',0,'2026-10-10T00:00:00.000Z',"
            "'2026-10-10T00:00:00.000Z')"
        )
        con.commit()
    finally:
        con.close()
    thread = store.get_thread("t9", db=db)
    assert thread.status is ThreadState.IDLE
    assert thread.origin is ThreadOrigin.IDE
