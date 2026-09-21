"""Unit tests: the oversight inbox API and rule-by-fatigue calibration.

Hermetic: a tmp SQLite DB per test, seeded through the real enqueue/resolve API
(the module self-bootstraps the schema). No lane, no network, no live run.

The editable install can resolve ``mini_ork`` to MAIN rather than this worktree,
so the repo root is inserted on ``sys.path`` before the imports.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates.oversight_inbox import (  # noqa: E402
    enqueue,
    get,
    pending,
    resolve,
)
from mini_ork.learning.oversight_calibration import (  # noqa: E402
    calibrate,
    summarize,
)


def _seed_resolved(db: str, gate: str, n: int, note: str = "") -> list[int]:
    """``n`` items in ``gate``, each resolved ``approved`` with ``note``."""
    ids = []
    for _ in range(n):
        inbox_id = enqueue(gate, "feat", phase="review",
                           context={"k": "v"}, db_path=db)
        assert resolve(inbox_id, "approved", review_note=note, db_path=db) is True
        ids.append(inbox_id)
    return ids


def test_1_enqueue_round_trips_through_get(tmp_path):
    db = str(tmp_path / "state.db")
    first = enqueue("human-review", "feat-a", phase="review",
                    context={"chapter": 7}, db_path=db)
    second = enqueue("human-review", "feat-b", db_path=db)
    assert second > first  # ids increase

    row = get(first, db_path=db)
    assert row is not None
    assert row["gate_id"] == "human-review"
    assert row["feature"] == "feat-a"
    assert row["phase"] == "review"
    assert row["context"] == {"chapter": 7}
    assert row["status"] == "pending"
    assert get(9999, db_path=db) is None


def test_2_enqueue_requires_gate_and_feature(tmp_path):
    db = str(tmp_path / "state.db")
    with pytest.raises(ValueError):
        enqueue("", "feat", db_path=db)
    with pytest.raises(ValueError):
        enqueue("   ", "feat", db_path=db)
    with pytest.raises(ValueError):
        enqueue("gate", "", db_path=db)


def test_3_resolve_is_one_way_and_status_is_checked(tmp_path):
    db = str(tmp_path / "state.db")
    inbox_id = enqueue("human-review", "feat", db_path=db)

    assert resolve(inbox_id, "approved", review_note="first", db_path=db) is True
    # One-way: a second resolve changes nothing and reports False, and the
    # first note is the one that survives.
    assert resolve(inbox_id, "rejected", review_note="second", db_path=db) is False
    row = get(inbox_id, db_path=db)
    assert row["status"] == "approved"
    assert row["review_note"] == "first"

    # 'pending' is not an outcome; neither is an invented status.
    other = enqueue("human-review", "feat", db_path=db)
    with pytest.raises(ValueError):
        resolve(other, "pending", db_path=db)
    with pytest.raises(ValueError):
        resolve(other, "maybe", db_path=db)
    assert get(other, db_path=db)["status"] == "pending"


def test_4_pending_returns_only_pending_oldest_first(tmp_path):
    db = str(tmp_path / "state.db")
    a = enqueue("human-review", "feat", db_path=db)
    b = enqueue("human-review", "feat", db_path=db)
    c = enqueue("human-review", "feat", db_path=db)
    assert resolve(b, "approved", db_path=db) is True

    rows = pending(db_path=db)
    assert [r["inbox_id"] for r in rows] == [a, c]


def test_5_fatigue_signature_is_high_rate_with_silent_notes(tmp_path):
    db = str(tmp_path / "state.db")
    _seed_resolved(db, "human-review", 10, note="")

    report = calibrate(db_path=db)
    assert report["n"] == 10
    gate = report["gates"][0]
    assert gate["enqueued"] == 10
    assert gate["resolved"] == 10
    assert gate["resolution_rate"] == 1.0
    assert gate["note_rate"] == 0.0
    assert gate["fatigue"] is True


def test_6_notes_carrying_information_clears_fatigue(tmp_path):
    db = str(tmp_path / "state.db")
    _seed_resolved(db, "human-review", 10, note="read it, it is fine")

    gate = calibrate(db_path=db)["gates"][0]
    assert gate["note_rate"] == 1.0
    assert gate["resolution_rate"] == 1.0
    assert gate["fatigue"] is False


def test_7_abandoned_is_a_repeated_enqueue_with_no_resolution(tmp_path):
    db = str(tmp_path / "state.db")
    for _ in range(6):
        enqueue("orphan-gate", "feat", db_path=db)

    gate = calibrate(db_path=db)["gates"][0]
    assert gate["enqueued"] == 6
    assert gate["resolved"] == 0
    # Denominator is 6, so 0.0 is a real zero — not the None case.
    assert gate["resolution_rate"] == 0.0
    assert gate["abandoned"] is True
    assert gate["fatigue"] is False


def test_8_stale_is_a_backlog_older_than_the_threshold(tmp_path):
    db = str(tmp_path / "state.db")
    ids = [enqueue("slow-gate", "feat", db_path=db) for _ in range(2)]

    now = time.time()
    con = sqlite3.connect(db)
    con.executemany(
        "UPDATE mo_inbox_gates SET enqueued_at = ? WHERE inbox_id = ?",
        [(now - 100 * 3600, i) for i in ids],
    )
    con.commit()
    con.close()

    gate = calibrate(db_path=db, now=now)["gates"][0]
    assert gate["pending"] == 2
    assert gate["oldest_pending_seconds"] == 100 * 3600
    assert gate["stale"] is True


def test_9_empty_denominator_is_none_never_zero(tmp_path):
    db = str(tmp_path / "state.db")

    # A gate absent from the table is absent from the report — it does not
    # appear with fabricated zeroes.
    report = calibrate(db_path=db)
    assert report["n"] == 0
    assert report["gates"] == []
    assert report["since"] is None
    marker = "no gate inbox items recorded yet"
    assert summarize(report) == marker

    # A live gate always has at least one enqueued row.
    enqueue("human-review", "feat", db_path=db)
    live = calibrate(db_path=db)
    assert live["gates"] and all(g["enqueued"] > 0 for g in live["gates"])
    # Nothing resolved → note_rate is None (no data), not 0.0.
    assert live["gates"][0]["note_rate"] is None
    assert live["gates"][0]["median_resolve_seconds"] is None


def test_10_cli_subcommand_is_registered():
    from mini_ork.cli.main import SUBCOMMAND_REGISTRY

    assert "oversight" in SUBCOMMAND_REGISTRY
