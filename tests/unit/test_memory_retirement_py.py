"""Unit tests: the reversible retirement lifecycle layered on the semantic
memory win/loss ledger.

Hermetic — a fresh tmp ``db_path`` per test, no network, no lane, no model.
``uses`` / ``wins`` are set by direct SQL so the number under assertion is
exactly the number written, never an inference about the ledger.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.memory import (
    RETIRE_ENTER_UTILITY,
    RETIRE_EXIT_UTILITY,
    RETIRE_MIN_USES,
    candidates,
    rank_with_prior,
    reactivate,
    retire,
    retirement_state,
    search,
    upsert,
)
from mini_ork.memory.semantic import _connect, _utility


def _seed(db: str, scope: str, text: str, key: str, uses: int, wins: int) -> int:
    """Upsert one keyed memory, then pin its uses/wins by direct SQL."""
    mid = upsert(text, scope=scope, key=key, db_path=db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE semantic_memory SET uses = ?, wins = ? WHERE id = ?",
        (uses, wins, mid),
    )
    conn.commit()
    conn.close()
    return mid


# ─── 1. reversibility of the flag itself ────────────────────────────────────


def test_retire_then_reactivate_is_reversible(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("use the auth middleware", scope="eng", key="k1", db_path=db)

    assert retire(mid, "wrong guidance", evidence_ref="run-42", db_path=db) is True
    st = retirement_state(mid, db_path=db)
    assert st is not None
    assert st["retired"] is True
    assert st["reason"] == "wrong guidance"
    assert st["evidence"] == "run-42"

    assert reactivate(mid, db_path=db) is True
    st = retirement_state(mid, db_path=db)
    assert st is not None
    assert st["retired"] is False
    assert st["retired_at"] == 0
    assert st["reason"] == ""
    assert st["evidence"] == ""


# ─── 2. unknown ids ─────────────────────────────────────────────────────────


def test_unknown_id(tmp_path):
    db = str(tmp_path / "mem.db")
    assert retire(999, "reason", db_path=db) is False
    assert reactivate(999, db_path=db) is False
    assert retirement_state(999, db_path=db) is None


# ─── 3. re-retiring preserves the original stamp ────────────────────────────


def test_reretire_preserves_original(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("t", scope="eng", key="k", db_path=db)

    assert retire(mid, "first reason", evidence_ref="ev1", db_path=db) is True
    first = retirement_state(mid, db_path=db)
    assert first is not None

    assert retire(mid, "second reason", evidence_ref="ev2", db_path=db) is True
    second = retirement_state(mid, db_path=db)
    assert second is not None

    assert second["reason"] == "first reason"
    assert second["evidence"] == "ev1"
    assert second["retired_at"] == first["retired_at"]


# ─── 4. an empty reason is rejected ─────────────────────────────────────────


def test_retire_requires_reason(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("t", scope="eng", key="k", db_path=db)
    with pytest.raises(ValueError):
        retire(mid, "", db_path=db)
    with pytest.raises(ValueError):
        retire(mid, "   ", db_path=db)


# ─── 5. end-to-end suppression through the real retrieval path ──────────────


def test_search_suppresses_and_recovers(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("the dispatcher must resolve an empty home", scope="eng",
                 key="k1", db_path=db)

    assert any(r["memory_id"] == mid for r in search("empty home", scope="eng", db_path=db))
    assert retire(mid, "obsolete", db_path=db) is True
    assert not any(r["memory_id"] == mid for r in search("empty home", scope="eng", db_path=db))
    assert reactivate(mid, db_path=db) is True
    assert any(r["memory_id"] == mid for r in search("empty home", scope="eng", db_path=db))


# ─── 6. rank_with_prior also excludes a retired memory ──────────────────────


def test_rank_with_prior_excludes_retired(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("alpha fact", scope="eng", key="k1", db_path=db)

    assert any(r["memory_id"] == mid
               for r in rank_with_prior([(mid, 1.0)], scope="eng", db_path=db))
    retire(mid, "obsolete", db_path=db)
    assert not any(r["memory_id"] == mid
                   for r in rank_with_prior([(mid, 1.0)], scope="eng", db_path=db))


# ─── 7. the resurrection hazard: re-upsert must not clear the flag ──────────


def test_upsert_does_not_resurrect_retired(tmp_path):
    db = str(tmp_path / "mem.db")
    mid = upsert("original text", scope="eng", key="k1", db_path=db)
    retire(mid, "obsolete", db_path=db)

    mid2 = upsert("refreshed text", scope="eng", key="k1", db_path=db)
    assert mid2 == mid

    st = retirement_state(mid, db_path=db)
    assert st is not None
    assert st["retired"] is True
    assert st["reason"] == "obsolete"
    assert not any(r["memory_id"] == mid
                   for r in search("refreshed text", scope="eng", db_path=db))


# ─── 8. candidates hysteresis ───────────────────────────────────────────────


def test_candidates_hysteresis(tmp_path):
    db = str(tmp_path / "mem.db")
    m_retire = _seed(db, "eng", "always wrong", "k1", 6, 0)   # utility 0.125 < enter
    m_ok = _seed(db, "eng", "always right", "k2", 6, 6)        # utility 0.875
    m_recover = _seed(db, "eng", "recovered", "k3", 6, 6)      # utility 0.875 > exit
    retire(m_recover, "was wrong then", db_path=db)
    m_new = _seed(db, "eng", "too new", "k4", 2, 0)            # below min_uses

    rows = candidates("eng", db_path=db)
    by_id = {r["memory_id"]: r for r in rows}

    assert m_retire in by_id
    assert by_id[m_retire]["state"] == "active"
    assert by_id[m_retire]["recommendation"] == "retire"

    assert m_ok not in by_id

    assert m_recover in by_id
    assert by_id[m_recover]["state"] == "retired"
    assert by_id[m_recover]["recommendation"] == "reactivate"

    assert m_new not in by_id

    # sorted by utility ascending
    utils = [r["utility"] for r in rows]
    assert utils == sorted(utils)


# ─── 9. non-asymmetric hysteresis is rejected ───────────────────────────────


def test_candidates_rejects_non_asymmetric(tmp_path):
    db = str(tmp_path / "mem.db")
    with pytest.raises(ValueError):
        candidates("eng", db_path=db, enter=RETIRE_EXIT_UTILITY, exit=RETIRE_EXIT_UTILITY)
    with pytest.raises(ValueError):
        candidates("eng", db_path=db, enter=0.5, exit=0.4)


# ─── 10. the guarded ALTER adds the retire columns to an older DB ───────────


def test_ensure_columns_adds_retire_columns(tmp_path):
    db_path = str(tmp_path / "mem.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE semantic_memory ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " scope TEXT NOT NULL, text TEXT NOT NULL, embedding BLOB NOT NULL,"
        " created_at REAL NOT NULL, meta TEXT,"
        " uses INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()

    conn = _connect(db_path)  # bootstrap: _BOOTSTRAP_SQL + _ensure_columns
    cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memory)")}
    conn.close()

    assert {"retired_at", "retire_reason", "retire_evidence"} <= cols


# ─── 11. the CLI is registered ──────────────────────────────────────────────


def test_cli_registered():
    from mini_ork.cli.main import SUBCOMMAND_REGISTRY
    assert "memory-lifecycle" in SUBCOMMAND_REGISTRY


# ─── constants exist as documented ──────────────────────────────────────────


def test_module_constants():
    assert RETIRE_ENTER_UTILITY < RETIRE_EXIT_UTILITY
    assert RETIRE_MIN_USES > 0
    assert _utility(6, 0) == 0.125
