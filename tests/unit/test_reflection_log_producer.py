"""The reflection pass queues real drift into ``reflection_log``.

The kickoff's non-vacuity check: a static ``INSERT`` literal alone closes
``db:L2`` without proving anything runs, so this seeds one stale record through
a *production* writer (``store.put_arch_spec``), ages the timestamp column the
stale query actually reads, and drives the real entry point
(``reflection_pipeline.reflection_run``). Nothing here inserts into
``reflection_log`` directly and no migration seeds it.

Hermetic: every test gets its own ``init_db`` database in ``tmp_path`` and
turns the LLM-backed extract step off.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.learning import reflection_pipeline
from mini_ork.memory import store
from mini_ork.stores.migrate import init_db

_REPO_ROOT = Path(store.__file__).resolve().parents[2]

_SECONDS_PER_DAY = 86400


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, fully migrated state DB wired through the env contract."""
    path = tmp_path / "state.db"
    rc, _out, err = init_db(db=str(path), root=str(_REPO_ROOT))
    assert rc == 0, err

    monkeypatch.setenv("MINI_ORK_DB", str(path))
    monkeypatch.setenv("MINI_ORK_STALE_DAYS", "14")
    monkeypatch.delenv("MO_REFLECT_POST_RUN", raising=False)
    # Step [1/6] dispatches an LLM per eligible trace; a unit test has none.
    monkeypatch.setenv("MO_REFLECTION_EXTRACT_GRADIENTS", "0")
    return str(path)


def _reflection_rows(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in con.execute(
                "SELECT * FROM reflection_log ORDER BY log_id"
            ).fetchall()
        ]
    finally:
        con.close()


def _seed_stale_adr(db: str, adr_id: str, *, age_days: int = 40) -> None:
    """Write through the production writer, then age the row it wrote.

    ``adrs`` has no ``updated_at``/``created_at``, so
    ``_detect_stale_select`` falls to ``reflection_last_check`` — age that.
    """
    store.put_adr(adr_id, "", f"title {adr_id}", "pre", "post", "ver", "body")
    stale_at = int(time.time()) - age_days * _SECONDS_PER_DAY
    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE adrs SET reflection_last_check = ? WHERE adr_id = ?",
            (stale_at, adr_id),
        )
        con.commit()
    finally:
        con.close()


def test_reflection_run_enqueues_the_seeded_stale_record(db):
    """One row, for the real stale id — not one per table, not the fresh row."""
    _seed_stale_adr(db, "ADR-STALE-1")
    store.put_adr("ADR-FRESH-1", "", "fresh", "pre", "post", "ver", "body")

    before = int(time.time())
    suggestions = reflection_pipeline.reflection_run(db_path=db)
    after = int(time.time())

    assert isinstance(suggestions, str)

    rows = _reflection_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["item_table"] == "adrs"
    assert row["item_id"] == "ADR-STALE-1"
    assert row["trigger"] == "manual"
    assert row["trigger_sha"] is None
    assert row["processed_at"] is None
    assert row["outcome"] is None
    assert before <= row["enqueued_at"] <= after

    drift = json.loads(row["drift_summary_json"])
    assert drift["table"] == "adrs"
    assert drift["stale_ids"] == ["ADR-STALE-1"]


def test_post_run_marker_queues_a_commit_trigger(db, monkeypatch):
    """MO_REFLECT_POST_RUN=1 is the engine's post-run marker → 'commit'."""
    _seed_stale_adr(db, "ADR-STALE-2")
    monkeypatch.setenv("MO_REFLECT_POST_RUN", "1")

    reflection_pipeline.reflection_run(db_path=db)

    rows = _reflection_rows(db)
    assert [r["trigger"] for r in rows] == ["commit"]
