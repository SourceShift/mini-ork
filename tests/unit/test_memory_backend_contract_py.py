"""Contract suite for the memory backend seam (design doc §6).

One suite, parametrized over :class:`SqlMemoryBackend` and
:class:`CnMemoryBackend` driven by a recorded HTTP fake — no live ContextNest,
no socket. The CN half monkeypatches ``mini_ork.cn_client``'s four surfaces, so
the seam still routes through ``cn_client`` exactly as production does.

The file-backed ping cache is shared across processes and tests; every test that
can drive ``available()`` points ``cn_client._ping_cache_file()`` at a tmp path
first, or a cached up/down from an earlier test makes the result
order-dependent.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import cn_client  # noqa: E402
from mini_ork import context_assembler  # noqa: E402
from mini_ork.memory import backends, semantic  # noqa: E402
from mini_ork.memory.backends import CnMemoryBackend, SqlMemoryBackend  # noqa: E402


class _RecordedCN:
    """A recorded HTTP fake for the four cn_client surfaces the seam uses."""

    def __init__(self) -> None:
        self.stores: dict[str, str] = {}
        self.retrieve_calls: list[tuple] = []
        self.outcomes: list[tuple] = []
        self.discards: list[tuple] = []
        self._next = 1

    def available(self) -> bool:
        return True

    def retrieve(self, query: str, limit: int = 8) -> str:
        self.retrieve_calls.append((query, limit))
        return json.dumps({"hits": [
            {"id": "atom-1", "content": "stored fact", "similarity": 0.91},
        ]})

    def store(self, content: str, *, session_id: str = "",
              importance=None, metadata=None) -> str:
        key = (metadata or {}).get("key") or content
        if key not in self.stores:
            self.stores[key] = f"atom-{self._next}"
            self._next += 1
        return json.dumps({"atom_id": self.stores[key]})

    def settle_outcome_batch(self, outcome: str, atom_ids, **kwargs) -> dict:
        self.outcomes.append((outcome, list(atom_ids)))
        return {"status": "ok", "updated": len(list(atom_ids))}

    def discard(self, atom_ids, reason: str = "") -> str:
        self.discards.append((list(atom_ids), reason))
        return json.dumps({"discarded": len(list(atom_ids))})


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def sql_backend(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    return SqlMemoryBackend(db_path=str(db)), {"kind": "sql", "db": str(db)}


@pytest.fixture
def cn_backend(tmp_path, monkeypatch):
    fake = _RecordedCN()
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("MO_MEMORY_BACKEND", "cn")
    # Isolate the shared file-backed ping cache before any available() drive.
    monkeypatch.setattr(cn_client, "_ping_cache_file", lambda *a, **k: tmp_path / "cn-ping.cache")
    monkeypatch.setattr(cn_client, "available", fake.available)
    monkeypatch.setattr(cn_client, "retrieve", fake.retrieve)
    monkeypatch.setattr(cn_client, "store", fake.store)
    monkeypatch.setattr(cn_client, "settle_outcome_batch", fake.settle_outcome_batch)
    monkeypatch.setattr(cn_client, "discard", fake.discard)
    return CnMemoryBackend(db_path=str(tmp_path / "state.db")), {"kind": "cn", "fake": fake}


@pytest.fixture(params=["sql", "cn"])
def backend(request):
    """The doc §6 suite, run over both backends."""
    if request.param == "sql":
        return request.getfixturevalue("sql_backend")
    return request.getfixturevalue("cn_backend")


# --------------------------------------------------------------------------
# §6 contract
# --------------------------------------------------------------------------


def test_upsert_is_idempotent_under_repeated_writes(backend):
    b, _info = backend
    first = b.upsert("the daemon stores this once", scope="contract", key="row-1")
    second = b.upsert("the daemon stores this once", scope="contract", key="row-1")
    assert first == second, "a repeated identical write must resolve to one record"


def test_recall_returns_hits_for_the_query(backend):
    b, info = backend
    if info["kind"] == "cn":
        hits = b.recall("anything", scope="contract", k=3)
        assert hits and hits[0]["text"] == "stored fact"
        assert info["fake"].retrieve_calls == [("anything", 3)]
    else:
        b.upsert("a fact worth recalling", scope="contract", key="r1")
        hits = b.recall("a fact worth recalling", scope="contract", k=3)
        assert any("worth recalling" in h["text"] for h in hits)


def test_recall_never_returns_a_retired_record(sql_backend):
    b, info = sql_backend
    from mini_ork.memory import retirement

    mid = b.upsert("retired fact", scope="contract", key="retire-1")
    assert retirement.retire(mid, "obsolete", db_path=info["db"]) is True
    assert b.recall("retired fact", scope="contract") == []


def test_record_use_moves_utility_off_neutral_after_one_success(backend):
    b, info = backend
    mid = b.upsert("a useful fact", scope="contract", key="use-1")
    if info["kind"] == "sql":
        assert b.record_use([mid], "win", scope="contract") == 1
        hits = b.recall("a useful fact", scope="contract")
        assert hits, "the fact must still be recallable after a use"
        # one win from one use → Laplace-smoothed utility 2/3, above neutral.
        assert hits[0]["utility"] > 0.5
    else:
        assert b.record_use([str(mid)], "win", run_id="run-x") == 1
        assert info["fake"].outcomes == [("success", [str(mid)])]


def test_pending_fallback_round_trip_is_idempotent(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    monkeypatch.setenv("MO_MEMORY_BACKEND", "auto")
    monkeypatch.setattr(cn_client, "_ping_cache_file", lambda *a, **k: tmp_path / "cn-ping.cache")

    backend = backends.resolve_backend()
    assert isinstance(backend, SqlMemoryBackend)
    mid = backend.upsert("written while CN was down", scope="contract", key="off-1")

    # A "restart" — a fresh read through a new connection — still sees the mark.
    pending = semantic.pending_records(db_path=str(db))
    assert [r["id"] for r in pending] == [mid]
    assert pending[0]["key"] == "off-1"

    # Clearing is the successful-replay step; doing it twice is harmless.
    assert semantic.clear_pending(mid, db_path=str(db)) is True
    assert semantic.clear_pending(mid, db_path=str(db)) is False
    assert semantic.pending_records(db_path=str(db)) == []


def test_list_kind_returns_newest_first_and_respects_the_namespace(sql_backend):
    b, _info = sql_backend
    older = b.upsert("older", scope="ns-a", key="a")
    newer = b.upsert("newer", scope="ns-a", key="b")
    b.upsert("elsewhere", scope="ns-b", key="c")

    rows = b.list_kind("semantic", scope="ns-a")
    assert [r["id"] for r in rows] == [newer, older]
    assert {r["scope"] for r in rows} == {"ns-a"}


def test_cn_list_kind_is_an_honest_gap():
    with pytest.raises(NotImplementedError) as excinfo:
        CnMemoryBackend().list_kind("semantic")
    message = str(excinfo.value)
    assert "memory-backend.md" in message and "4.1.1" in message


def test_strict_cn_recall_raises_when_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("MO_MEMORY_BACKEND", "cn")
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    monkeypatch.setattr(cn_client, "_ping_cache_file", lambda *a, **k: tmp_path / "cn-ping.cache")

    backend = backends.resolve_backend()
    assert isinstance(backend, CnMemoryBackend)
    with pytest.raises(backends.MemoryBackendUnavailable):
        backend.recall("anything")


def test_auto_falls_back_to_sql_when_cn_is_down(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("MO_MEMORY_BACKEND", "auto")
    monkeypatch.setenv("MO_DISABLE_CN", "1")
    monkeypatch.setattr(cn_client, "_ping_cache_file", lambda *a, **k: tmp_path / "cn-ping.cache")

    with caplog.at_level(logging.INFO, logger=backends.__name__):
        backend = backends.resolve_backend()
    assert isinstance(backend, SqlMemoryBackend)
    selection = [r.getMessage().lower() for r in caplog.records]
    assert any("sql" in m and "fallback" in m for m in selection)


def test_memory_backend_statement_names_the_backend(monkeypatch):
    monkeypatch.setenv("MO_MEMORY_BACKEND", "sql")
    assert context_assembler.memory_backend_statement() == "Memory backend: sql."
    assert "fallback" in context_assembler.memory_backend_statement(
        "sql", "cn_unavailable"
    )


# --------------------------------------------------------------------------
# auto must degrade, not fail: a reachable daemon whose retrieve times out is
# the measured case (2026-11-25: 17.5 s retrieve against an 8 s read budget).
# --------------------------------------------------------------------------


def _patch_cn(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(cn_client, "_ping_cache_file",
                        lambda *a, **k: tmp_path / "cn-ping.cache")
    for name in ("available", "retrieve", "store", "settle_outcome_batch",
                 "discard"):
        monkeypatch.setattr(cn_client, name, getattr(fake, name))


def test_auto_serves_sql_when_a_cn_read_fails(monkeypatch, tmp_path, caplog):
    fake = _RecordedCN()
    fake.retrieve = lambda q, limit=8: "{}"   # daemon up, retrieve produced nothing
    _patch_cn(monkeypatch, tmp_path, fake)
    monkeypatch.setenv("MO_MEMORY_BACKEND", "auto")

    backend = backends.resolve_backend()
    assert isinstance(backend, backends.FallbackMemoryBackend)

    with caplog.at_level(logging.WARNING, logger=backends.__name__):
        hits = backend.recall("anything", scope="contract", k=2)

    assert isinstance(hits, list), "a failed CN read must not raise in auto mode"
    assert any("sql" in r.getMessage().lower() for r in caplog.records), \
        "the fallback must be logged, never silent"


def test_auto_marks_writes_pending_when_the_cn_write_fails(monkeypatch, tmp_path):
    import sqlite3

    fake = _RecordedCN()
    fake.store = lambda content, **kw: "{}"    # daemon up, store produced nothing
    _patch_cn(monkeypatch, tmp_path, fake)
    monkeypatch.setenv("MO_MEMORY_BACKEND", "auto")

    backend = backends.resolve_backend()
    backend.upsert("a fact learned while CN was answering badly", key="pending-1")

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT pending_cn FROM semantic_memory WHERE scope = ?",
            ("semantic",),
        ).fetchone()
    finally:
        con.close()
    assert row is not None, "the fallback must still store the fact"
    assert row[0] == 1, "an unsynced fallback write must be marked for replay"
