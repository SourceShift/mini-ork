"""Tests for mini_ork.memory — standalone, opt-in semantic long-term memory.

Each named test corresponds to one DoD bullet from the kickoff so the verifier
can mechanically confirm coverage. The ``patch_dispatch`` fixture substitutes
``mini_ork.memory.semantic.dispatch_model`` (the import site inside the
module) with a stub that returns a caller-controlled JSON list, so no real
provider is invoked and the suite is hermetic + zero-cost.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.dispatch import DispatchResult
from mini_ork.memory import add, search


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / "0046_semantic_memory.sql"


@pytest.fixture
def patch_dispatch(monkeypatch):
    """Patch ``mini_ork.memory.semantic.dispatch_model`` to return a stubbed
    JSON list. Pass a string (raw response), a list of strings, or a list of
    ``{"text": ..., "op": ...}`` dicts — the stub JSON-encodes whatever it's
    given and the module's normalizer accepts all three shapes.
    """

    state = {"response_text": json.dumps([{"text": "default fact", "op": "add"}])}

    def _set(response):
        if isinstance(response, (list, dict)):
            state["response_text"] = json.dumps(response)
        else:
            state["response_text"] = response

    def _stub(_request):
        return DispatchResult(
            ok=True, rc=0, text=state["response_text"], model="stub",
        )

    monkeypatch.setattr("mini_ork.memory.semantic.dispatch_model", _stub)
    return _set


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "semantic_memory_test.db"


# ── DoD #1: add() with infer=True extracts a fact, search() ranks it ───────


def test_add_infers_and_search_ranks(patch_dispatch, db_path):
    """add() with infer=True calls the model to extract a fact, stores it
    in scope, and search() for a related query returns it ranked above an
    unrelated memory in the same scope."""
    # Related fact (the target).
    patch_dispatch(["User prefers dark mode for the dashboard."])
    add("random user text", scope="ux", infer=True, db_path=db_path)

    # Unrelated fact in the same scope — the related one must still rank higher.
    patch_dispatch(["Cats make excellent office companions."])
    add("more random text", scope="ux", infer=True, db_path=db_path)

    results = search("dark mode preference", scope="ux", db_path=db_path)
    assert len(results) >= 2, f"expected ≥2 results, got {results}"
    # Top hit is the related memory, not the unrelated one.
    assert "dark mode" in results[0]["text"].lower()
    # Score is monotonically non-increasing.
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True), f"not sorted desc: {scores}"
    # And the related memory is ranked above the unrelated one.
    related_score = next(r["score"] for r in results if "dark mode" in r["text"].lower())
    unrelated_score = next(
        r["score"] for r in results if "cats" in r["text"].lower()
    )
    assert related_score > unrelated_score, (
        f"related {related_score} should beat unrelated {unrelated_score}"
    )


# ── DoD #2: reconcile prevents unbounded growth on re-add of same fact ────


def test_reconcile_prevents_unbounded_growth(patch_dispatch, db_path):
    """Re-adding the same fact N times produces a single row (UPDATE in
    place, not blind INSERT). The no-unbounded-growth guarantee."""
    patch_dispatch(["User's favorite color is blue"])

    n_repeats = 10
    for _ in range(n_repeats):
        add("random text", scope="color", infer=True, db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM semantic_memory WHERE scope = ?", ("color",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1, (
        f"expected 1 row after {n_repeats} re-adds, got {count} — "
        f"reconcile did not prevent unbounded growth"
    )


# ── DoD #3: reconcile emits UPDATE (or DELETE+ADD) on overlap, not blind ADD


def test_reconcile_emits_update_or_delete_add(patch_dispatch, db_path):
    """A re-add (high cosine overlap) emits an UPDATE event, never a blind
    ADD. The memory_id stays the same so the row is not duplicated."""
    patch_dispatch(["User lives in Berlin"])
    events_initial = add("text A", scope="loc", infer=True, db_path=db_path)
    assert len(events_initial) == 1
    assert events_initial[0]["op"] == "ADD"
    initial_id = events_initial[0]["memory_id"]

    # Re-add the same fact — should UPDATE in place (cosine ~ 1.0).
    patch_dispatch(["User lives in Berlin"])
    events_second = add("text B", scope="loc", infer=True, db_path=db_path)
    assert len(events_second) == 1, f"expected 1 event, got {events_second}"
    assert events_second[0]["op"] in ("UPDATE", "DELETE"), (
        f"expected UPDATE or DELETE+ADD, got {events_second[0]['op']}"
    )
    # The memory_id is preserved across the UPDATE.
    assert events_second[0]["memory_id"] == initial_id, (
        f"UPDATE must preserve memory_id, "
        f"initial={initial_id} second={events_second[0]['memory_id']}"
    )

    # And the underlying row count is still 1 — not a blind ADD.
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM semantic_memory WHERE scope = ?", ("loc",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1, f"expected 1 row after UPDATE, got {count}"


# ── DoD #4: search is scope-scoped ────────────────────────────────────────


def test_search_is_scoped(patch_dispatch, db_path):
    """Memories in scope A do not leak into scope-B queries (and vice versa)."""
    patch_dispatch(["User lives in Berlin"])
    add("text", scope="loc", infer=True, db_path=db_path)

    patch_dispatch(["User owns a red car"])
    add("text", scope="vehicle", infer=True, db_path=db_path)

    # scope "loc" query returns only the Berlin memory.
    loc_results = search("city residence", scope="loc", db_path=db_path)
    assert loc_results, "expected at least one result for scope=loc"
    for r in loc_results:
        assert "berlin" in r["text"].lower(), (
            f"scope leak: {r['text']!r} returned for scope=loc"
        )

    # scope "vehicle" query returns only the car memory.
    vehicle_results = search("automobile", scope="vehicle", db_path=db_path)
    assert vehicle_results, "expected at least one result for scope=vehicle"
    for r in vehicle_results:
        assert "car" in r["text"].lower(), (
            f"scope leak: {r['text']!r} returned for scope=vehicle"
        )

    # And the two scopes are disjoint.
    loc_ids = {r["memory_id"] for r in loc_results}
    vehicle_ids = {r["memory_id"] for r in vehicle_results}
    assert loc_ids.isdisjoint(vehicle_ids), (
        f"scope ids overlap: {loc_ids & vehicle_ids}"
    )

    # Empty/whitespace scope is rejected.
    with pytest.raises(ValueError):
        search("anything", scope="", db_path=db_path)
    with pytest.raises(ValueError):
        search("anything", scope="   ", db_path=db_path)


# ── DoD #5: migration SQL is idempotent (apply twice, no error) ───────────


def test_migration_idempotent(db_path):
    """Applying the canonical migration SQL twice in a row must not raise.
    The table/index exist after the first apply; the IF NOT EXISTS guards
    make the second apply a no-op."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS semantic_memory" in sql, (
        "migration SQL missing the canonical CREATE TABLE"
    )
    assert "CREATE INDEX IF NOT EXISTS idx_semantic_memory_scope" in sql, (
        "migration SQL missing the canonical scope index"
    )

    conn = sqlite3.connect(str(db_path))
    try:
        # The migration file also writes to schema_migrations, which is a
        # real table in the runtime DB but doesn't exist in a fresh test DB.
        # Pre-create the bookkeeping table so the INSERT in the migration
        # doesn't fail — production runs hit a DB that already has it.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
              filename   TEXT PRIMARY KEY,
              applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              checksum   TEXT
            );
        """)
        # First apply.
        conn.executescript(sql)
        conn.commit()
        # Second apply — must not raise.
        conn.executescript(sql)
        conn.commit()
        # And the schema is exactly what the contract promised.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(semantic_memory)")}
        assert cols == {"id", "scope", "text", "embedding", "created_at", "meta"}, (
            f"unexpected columns: {cols}"
        )
        # The scope index exists.
        idx_names = {
            row[1] for row in conn.execute("PRAGMA index_list(semantic_memory)")
        }
        assert "idx_semantic_memory_scope" in idx_names, (
            f"scope index missing; got {idx_names}"
        )
    finally:
        conn.close()
