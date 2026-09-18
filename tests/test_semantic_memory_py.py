"""Tests for mini_ork.memory — semantic long-term memory with a utility-aware
retrieval policy.

Each named test corresponds to one DoD bullet from the kickoff so the verifier
can mechanically confirm coverage; the SimUtil-UCB section below covers the
retrieval-policy tranche (RetroAgent 2603.08561). The ``patch_dispatch``
fixture substitutes ``mini_ork.memory.semantic.dispatch_model`` (the import
site inside the module) with a stub that returns a caller-controlled JSON
list, so no real provider is invoked and the suite is hermetic + zero-cost.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from mini_ork.dispatch import DispatchResult
from mini_ork.memory import (
    add,
    rank_with_prior,
    record_outcome,
    record_retrievals,
    search,
)
from mini_ork.memory.semantic import _connect as _semantic_connect
from mini_ork.memory.semantic import _pack_embedding as _pack_vector
from mini_ork.stores.migrate import migrate_apply


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MIGRATION_PATH = MIGRATIONS_DIR / "0046_semantic_memory.sql"
UTILITY_MIGRATION_PATH = MIGRATIONS_DIR / "0055_semantic_memory_utility.sql"
ATTRIBUTION_MIGRATION_PATH = (
    MIGRATIONS_DIR / "0058_semantic_memory_attribution.sql"
)

# The ledger contract as of 0046 + 0055, before attribution (0058) widened it.
_LEDGER_COLUMNS_AT_0055 = {
    "id", "memory_id", "scope", "run_id", "task_class", "retrieved_at", "outcome",
}


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


def test_migration_idempotent(tmp_path, db_path):
    """Applying the canonical migration SQL — 0046 then the 0055 utility
    tranche — twice in a row must not raise, and must leave exactly the
    schema both files promise.

    Runs through the real loader (``migrate_apply``), not a raw
    ``executescript``: 0055 carries a `.read "|sh -c …"` guarded ALTER, which
    only the loader's dot-command interpreter can execute. Re-applying is the
    case that matters — a bare ADD COLUMN would die on "duplicate column", so
    this test is what pins the guard.
    """
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    util_sql = UTILITY_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS semantic_memory" in sql, (
        "migration SQL missing the canonical CREATE TABLE"
    )
    assert "CREATE INDEX IF NOT EXISTS idx_semantic_memory_scope" in sql, (
        "migration SQL missing the canonical scope index"
    )
    assert "CREATE TABLE IF NOT EXISTS semantic_memory_uses" in util_sql, (
        "0055 missing the retrieval ledger table"
    )

    # The loader applies every *.sql in the dir in lex order, so an isolated
    # dir with just these two keeps the test independent of the other ~50
    # migrations.
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    shutil.copy(MIGRATION_PATH, migrations / MIGRATION_PATH.name)
    shutil.copy(UTILITY_MIGRATION_PATH, migrations / UTILITY_MIGRATION_PATH.name)

    rc, out = migrate_apply(str(migrations), db=str(db_path))
    assert rc == 0, f"first apply failed (rc={rc}): {out}"

    # A plain second `migrate_apply` would short-circuit on schema_migrations
    # and prove nothing. Clear the bookkeeping so the loader genuinely re-runs
    # both files against a table that ALREADY has the columns — which is the
    # only case the `.read "|sh -c …"` guard exists for. Without it, the bare
    # `ALTER TABLE … ADD COLUMN uses` dies with "duplicate column name: uses".
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
    finally:
        conn.close()

    rc, out = migrate_apply(str(migrations), db=str(db_path))
    assert rc == 0, f"re-apply failed (rc={rc}): {out}"

    conn = sqlite3.connect(str(db_path))
    try:
        # And the schema is exactly what the contract promised — the six 0046
        # columns plus the two utility counters.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(semantic_memory)")}
        assert cols == {
            "id", "scope", "text", "embedding", "created_at", "meta",
            "uses", "wins",
        }, f"unexpected columns: {cols}"
        # The scope index exists.
        idx_names = {
            row[1] for row in conn.execute("PRAGMA index_list(semantic_memory)")
        }
        assert "idx_semantic_memory_scope" in idx_names, (
            f"scope index missing; got {idx_names}"
        )
        # The ledger exists with the outcome CHECK the writer relies on.
        ledger_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(semantic_memory_uses)")
        }
        assert ledger_cols == _LEDGER_COLUMNS_AT_0055, (
            f"unexpected ledger columns: {ledger_cols}"
        )
    finally:
        conn.close()


def test_attribution_migration_is_additive_and_idempotent(tmp_path, db_path):
    """0058 gives each retrieval event the identity of the decision that
    caused it. Two things must hold: the ledger widens by exactly the two
    accounting columns, and re-applying the file is a no-op rather than a
    "duplicate column" crash.

    The guard is the load-bearing part. 0058 is a *second* migration touching
    a table 0055 already widened, so a fresh apply and a re-apply hit
    different code paths — the fresh one adds the columns, the re-apply must
    find them and skip. Only the guarded `.read "|sh -c …"` idiom does both.
    """
    sql = ATTRIBUTION_MIGRATION_PATH.read_text(encoding="utf-8")
    # Assert on statements, not on prose — the header comment legitimately
    # contains the words "dropped" and "altered" while promising the opposite.
    code = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "semantic_memory_uses" in code, "0058 must target the retrieval ledger"
    assert "DROP" not in code.upper(), "0058 must not drop anything"
    assert "ALTER TABLE semantic_memory " not in code, (
        "0058 must not touch the memory table; only the ledger"
    )

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in (MIGRATION_PATH, UTILITY_MIGRATION_PATH, ATTRIBUTION_MIGRATION_PATH):
        shutil.copy(path, migrations / path.name)

    rc, out = migrate_apply(str(migrations), db=str(db_path))
    assert rc == 0, f"first apply failed (rc={rc}): {out}"

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
    finally:
        conn.close()

    rc, out = migrate_apply(str(migrations), db=str(db_path))
    assert rc == 0, f"re-apply failed (rc={rc}): {out}"

    conn = sqlite3.connect(str(db_path))
    try:
        ledger_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(semantic_memory_uses)")
        }
        idx_names = {
            row[1] for row in conn.execute("PRAGMA index_list(semantic_memory_uses)")
        }
    finally:
        conn.close()

    assert ledger_cols == _LEDGER_COLUMNS_AT_0055 | {"lane", "node_id"}, (
        f"0058 must add exactly lane + node_id; got {ledger_cols}"
    )
    assert "idx_semantic_memory_uses_lane" in idx_names, (
        f"lane index missing; got {idx_names}"
    )


# ── SimUtil-UCB: the retrieval policy (RetroAgent 2603.08561) ─────────────


class _FixedEmbedder:
    """Deterministic embedder with exact, hand-chosen vectors.

    The utility tests need *known* similarities, and HashEmbedder's buckets
    are opaque — pinning a vector per text makes every expected composite
    computable by hand, so an assertion failure names a number rather than a
    hash. Unknown text embeds to the zero vector (similarity 0 to
    everything), which is a legitimate "no opinion".
    """

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table

    def embed(self, texts):
        return [list(self.table.get(t, [0.0, 0.0])) for t in texts]


def _seed(db_path, scope, text, vec, *, uses=0, wins=0) -> int:
    """Insert a memory row directly, with a pinned vector and counters.

    ``add()`` cannot create these fixtures: its reconcile pass UPDATEs in
    place whenever an incoming fact is within ``UPDATE_THRESHOLD`` (0.90) of an
    existing one, and two unit vectors both close to the same query are
    necessarily nearly identical to each other — so the second add would
    overwrite the first. The ranking tests need rows that are *similar to the
    query and distinct from each other*, which is exactly the input shape the
    reconcile step is designed to collapse. Seeding is how they get it.

    Goes through the module's own ``_connect`` so the test never carries its
    own copy of the DDL to drift out of sync.
    """
    conn = _semantic_connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO semantic_memory(scope, text, embedding, created_at, uses, wins) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (scope, text, _pack_vector(vec), 0.0, uses, wins),
        )
        conn.commit()
    finally:
        conn.close()
    return int(cur.lastrowid or 0)


def _rows(db_path, scope):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, text, uses, wins FROM semantic_memory "
            "WHERE scope = ? ORDER BY id",
            (scope,),
        ).fetchall()
    finally:
        conn.close()


def _by_text(db_path, scope):
    return {text: (mid, uses, wins) for mid, text, uses, wins in _rows(db_path, scope)}


def test_similarity_alone_orders_when_no_history(db_path):
    """Baseline: with zero uses on both memories the composite reduces to
    similarity, so the closer memory wins. This is the control for the flip
    below — it proves the flip is caused by utility, not by the pool."""
    emb = _FixedEmbedder({"q": [1.0, 0.0]})
    _seed(db_path, "s", "near", [0.8, 0.6])    # unit → sim(q, near) = 0.8
    _seed(db_path, "s", "far", [0.6, -0.8])    # unit → sim(q, far)  = 0.6

    results = search("q", scope="s", top_k=1, db_path=db_path, embedder=emb)
    assert results[0]["text"] == "near"
    assert results[0]["similarity"] == pytest.approx(0.8)
    assert results[0]["utility"] == pytest.approx(0.5)  # untried → neutral
    assert results[0]["uses"] == 0 and results[0]["wins"] == 0


def test_utility_reorders_candidates_relevance_admitted(db_path):
    """A memory that has actually worked outranks a nearer one that has
    repeatedly failed. Same two vectors as the control above, so the only
    thing that changed is the track record — this is the whole point of
    SimUtil-UCB: similarity cannot tell these two apart, outcomes can.

    Arithmetic (20 resolved retrievals each, n_total = 40):
      near: sim 0.80, utility = 1/22, exploration = 0.10·sqrt(ln 41 / 21)
            → 0.80 + 0.30·(1/22 − 0.5) + 0.10·sqrt(ln 41 / 21)  ≈ 0.7057
      far:  sim 0.60, utility = 21/22, same exploration
            → 0.60 + 0.30·(21/22 − 0.5) + 0.10·sqrt(ln 41 / 21) ≈ 0.7784
    """
    emb = _FixedEmbedder({"q": [1.0, 0.0]})
    near_id = _seed(db_path, "s", "near", [0.8, 0.6])
    far_id = _seed(db_path, "s", "far", [0.6, -0.8])

    for i in range(20):
        record_retrievals([near_id], scope="s", run_id=f"near-{i}", db_path=db_path)
        record_outcome(f"near-{i}", False, db_path=db_path)
        record_retrievals([far_id], scope="s", run_id=f"far-{i}", db_path=db_path)
        record_outcome(f"far-{i}", True, db_path=db_path)

    results = search("q", scope="s", top_k=2, db_path=db_path, embedder=emb)
    assert [r["text"] for r in results] == ["far", "near"], (
        f"utility did not reorder: {[(r['text'], r['score']) for r in results]}"
    )
    far, near = results
    assert far["similarity"] < near["similarity"], "premise: far is the less similar one"
    assert far["wins"] == 20 and far["uses"] == 20
    assert near["wins"] == 0 and near["uses"] == 20
    assert far["utility"] > near["utility"]


def test_relevance_gates_the_pool(db_path):
    """Exploration may reorder candidates, but it must never *admit* one that
    similarity did not. The survey's own caution is that the UCB bonus
    "deliberately surfaces untested memories" — surfacing them inside the
    candidate pool is the feature; letting one outrank the pool is the bug.

    Setup: 5 near-identical memories (sim 0.2) carrying a poor record, and one
    dissimilar memory (sim 0.1) that has never been tried — so it holds the
    largest exploration bonus available. At top_k=1 the pool is the top 4 by
    similarity, and the untried memory's composite (~0.335) is higher than
    every admitted memory's (~0.089). If the pool gate were removed it would
    be returned first; the assertion is that it is absent.

    Then the same query at top_k=6 admits it — and it does place first, which
    is what proves the earlier absence was the gate and not the arithmetic.
    """
    emb = _FixedEmbedder({"q": [1.0, 0.0]})
    # Unit vectors; sim(q,·) is the first component. All five are the same
    # vector, which is only possible because _seed bypasses the reconcile.
    near_vec = [0.2, 0.9797959]     # 0.04 + 0.96 = 1.0
    for i in range(5):
        _seed(db_path, "s", f"near{i}", near_vec, uses=50)
    untried_id = _seed(db_path, "s", "untried", [0.1, 0.9949874])  # uses=0

    gated = search("q", scope="s", top_k=1, db_path=db_path, embedder=emb)
    assert len(gated) == 1
    assert gated[0]["text"].startswith("near"), (
        f"pool gate leaked a memory relevance excluded: {gated}"
    )
    assert gated[0]["similarity"] == pytest.approx(0.2, abs=1e-4)
    assert all(r["memory_id"] != untried_id for r in gated)

    # Widen the pool and the untried memory is admitted — and wins on its
    # exploration bonus alone. Same data, so the gate is the only difference.
    widened = search("q", scope="s", top_k=6, db_path=db_path, embedder=emb)
    assert len(widened) == 6, "the widened pool should admit every memory"
    assert widened[0]["memory_id"] == untried_id, (
        f"expected the untried memory to lead once admitted: {widened}"
    )
    assert widened[0]["similarity"] == pytest.approx(0.1, abs=1e-4)
    assert widened[0]["score"] > gated[0]["score"], (
        "premise: the gate excluded a memory that would have outranked the winner"
    )


def test_record_outcome_resolves_the_ledger_and_bumps_wins(db_path):
    """The full loop: retrieve → pending ledger row + uses bump; resolve with
    a pass → row becomes 'win' and wins bumps."""
    emb = _FixedEmbedder({"q": [1.0, 0.0], "m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    assert record_retrievals(
        [mid], scope="s", run_id="run-1", task_class="code-fix", db_path=db_path,
    ) == 1
    assert _by_text(db_path, "s")["m"][1:] == (1, 0), "uses bumps at retrieval"

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT scope, run_id, task_class, outcome FROM semantic_memory_uses",
        ).fetchone()
    finally:
        conn.close()
    assert row == ("s", "run-1", "code-fix", "pending")

    assert record_outcome("run-1", True, db_path=db_path) == 1
    assert _by_text(db_path, "s")["m"][1:] == (1, 1)

    conn = sqlite3.connect(str(db_path))
    try:
        outcome = conn.execute(
            "SELECT outcome FROM semantic_memory_uses WHERE run_id = 'run-1'",
        ).fetchone()[0]
    finally:
        conn.close()
    assert outcome == "win"

    # And the resolved win is visible to the next retrieval's ranking.
    res = search("q", scope="s", top_k=1, db_path=db_path, embedder=emb)
    assert res[0]["utility"] == pytest.approx(2 / 3)
    assert res[0]["wins"] == 1


def test_losing_run_does_not_bump_wins(db_path):
    """A loss closes the ledger row but leaves `wins` alone — `uses` was
    already counted at retrieval, and a loss is exactly "used, did not help"."""
    emb = _FixedEmbedder({"q": [1.0, 0.0], "m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    record_retrievals([mid], scope="s", run_id="bad", db_path=db_path)
    assert record_outcome("bad", False, db_path=db_path) == 1
    assert _by_text(db_path, "s")["m"][1:] == (1, 0)


def test_unattributed_retrieval_fails_closed(db_path):
    """A retrieval whose run never reports counts toward `uses` forever and
    never toward `wins`, so it can only make a memory look worse. The failure
    mode being guarded is a memory earning credit it never proved — silence
    must not read as permission."""
    emb = _FixedEmbedder({"q": [1.0, 0.0], "m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    record_retrievals([mid], scope="s", run_id="vanished", db_path=db_path)
    # No record_outcome() call — the run never reported.
    assert _by_text(db_path, "s")["m"][1:] == (1, 0)

    res = search("q", scope="s", top_k=1, db_path=db_path, embedder=emb)
    assert res[0]["wins"] == 0
    assert res[0]["utility"] == pytest.approx(1 / 3), "smoothed, but below neutral"
    assert res[0]["utility"] < 0.5


def test_record_outcome_is_idempotent(db_path):
    """Only 'pending' rows resolve, so a resumed or re-stamped run cannot
    double-count a win."""
    emb = _FixedEmbedder({"q": [1.0, 0.0], "m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    record_retrievals([mid], scope="s", run_id="run-1", db_path=db_path)
    assert record_outcome("run-1", True, db_path=db_path) == 1
    assert record_outcome("run-1", True, db_path=db_path) == 0
    assert record_outcome("run-1", False, db_path=db_path) == 0
    assert _by_text(db_path, "s")["m"][1:] == (1, 1), "wins must not double-count"


def test_record_retrievals_rejects_ids_outside_the_scope(db_path):
    """The ledger is the authoritative audit trail: it must never assert a
    retrieval of a memory that is absent, or of one belonging to a different
    scope — otherwise a caller could inflate another scope's counters."""
    emb = _FixedEmbedder({"q": [1.0, 0.0], "a": [1.0, 0.0], "b": [1.0, 0.0]})
    add("a", scope="one", infer=False, db_path=db_path, embedder=emb)
    add("b", scope="two", infer=False, db_path=db_path, embedder=emb)
    a_id = _by_text(db_path, "one")["a"][0]
    b_id = _by_text(db_path, "two")["b"][0]

    # b lives in another scope; 9999 does not exist at all.
    assert record_retrievals(
        [b_id, 9999], scope="one", run_id="x", db_path=db_path,
    ) == 0
    # The real one in scope is recorded; a repeat of the same id is deduped.
    assert record_retrievals(
        [a_id, a_id], scope="one", run_id="x", db_path=db_path,
    ) == 1

    assert _by_text(db_path, "one")["a"][1:] == (1, 0)
    assert _by_text(db_path, "two")["b"][1:] == (0, 0), "foreign scope untouched"
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM semantic_memory_uses",
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_retrieval_ledger_round_trips_across_connections(db_path):
    """The counters are a denormalized index over the ledger; a fresh
    connection (i.e. a fresh process) must see both, consistently."""
    emb = _FixedEmbedder({"q": [1.0, 0.0], "m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    for i in range(3):
        record_retrievals([mid], scope="s", run_id=f"r{i}", db_path=db_path)
    record_outcome("r0", True, db_path=db_path)
    record_outcome("r1", False, db_path=db_path)
    # r2 deliberately left pending.

    conn = sqlite3.connect(str(db_path))
    try:
        ids, uses, wins = conn.execute(
            "SELECT COUNT(*), SUM(uses), SUM(wins) FROM semantic_memory",
        ).fetchone()
        outcomes = dict(conn.execute(
            "SELECT outcome, COUNT(*) FROM semantic_memory_uses GROUP BY outcome",
        ).fetchall())
    finally:
        conn.close()

    assert (ids, uses, wins) == (1, 3, 1), "counter disagrees with the ledger"
    assert outcomes == {"win": 1, "loss": 1, "pending": 1}


def test_connect_upgrades_a_table_that_predates_the_utility_columns(db_path):
    """Every runtime DB created at 0046 (or by the module's own earlier
    bootstrap) has the table *without* `uses`/`wins`. `CREATE TABLE IF NOT
    EXISTS` will not add them, so existence of the table is not evidence the
    columns are there — search() must upgrade in place rather than blow up on
    an unknown column."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE semantic_memory (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              scope      TEXT    NOT NULL,
              text       TEXT    NOT NULL,
              embedding  BLOB    NOT NULL,
              created_at REAL    NOT NULL,
              meta       TEXT
            );
            CREATE INDEX idx_semantic_memory_scope ON semantic_memory(scope);
        """)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memory)")}
        assert "uses" not in cols, "premise: the pre-0055 table lacks the counters"
    finally:
        conn.close()

    # No explicit migration run and no embedder seeding — search() alone must
    # bring the table up to the current shape.
    emb = _FixedEmbedder({"q": [1.0, 0.0]})
    assert search("q", scope="s", db_path=db_path, embedder=emb) == []

    conn = sqlite3.connect(str(db_path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memory)")}
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
    finally:
        conn.close()
    assert {"uses", "wins"} <= cols, f"columns not upgraded: {cols}"
    assert "semantic_memory_uses" in tables, "ledger not bootstrapped"


def test_connect_upgrades_a_ledger_that_predates_the_attribution_columns(db_path):
    """The ledger has two upgrade points now: 0055 created it, 0058 widened it.
    A DB that reached 0055 and stopped must be brought to the current shape by
    the module's own bootstrap, because the migration loader is not the only
    path a live DB takes — ``_connect`` runs on every call.

    This is the second half of a contract that has to hold on both sides: the
    migration adds the columns to a DB that goes through the loader, and
    ``_ADDED_COLUMNS`` adds them to one that does not. If only one carried
    them, a migrated DB and a bootstrapped DB would disagree about whether
    attribution can be written at all.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE semantic_memory_uses (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              memory_id    INTEGER NOT NULL,
              scope        TEXT    NOT NULL,
              run_id       TEXT    NOT NULL DEFAULT '',
              task_class   TEXT    NOT NULL DEFAULT '',
              retrieved_at REAL    NOT NULL,
              outcome      TEXT    NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX idx_semantic_memory_uses_scope
              ON semantic_memory_uses(scope);
        """)
        conn.commit()
        have = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memory_uses)")}
        assert "lane" not in have, "premise: the pre-0058 ledger lacks attribution"
    finally:
        conn.close()

    # search() alone must bring the ledger up to the current shape.
    emb = _FixedEmbedder({"q": [1.0, 0.0]})
    assert search("q", scope="s", db_path=db_path, embedder=emb) == []

    conn = sqlite3.connect(str(db_path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(semantic_memory_uses)")}
    finally:
        conn.close()
    assert {"lane", "node_id"} <= cols, f"ledger columns not upgraded: {cols}"


# ── attribution: which decision caused the retrieval (LIMBO 2609.14138) ─────


def test_record_retrievals_attributes_the_decision_that_caused_it(db_path):
    """A retrieval event carries the routed lane and the node whose prompt was
    injected. Without it, memory spend cannot be held to the decision that
    incurred it — a lane could retrieve heavily, fail, and show up in the
    aggregate as if it had been frugal."""
    emb = _FixedEmbedder({"q": [1.0, 0.0], "m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    assert record_retrievals(
        [mid], scope="s", run_id="run-1", task_class="code-fix",
        lane="frontier", node_id="implementer-2", db_path=db_path,
    ) == 1

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT run_id, task_class, lane, node_id, outcome "
            "FROM semantic_memory_uses",
        ).fetchone()
    finally:
        conn.close()
    assert row == ("run-1", "code-fix", "frontier", "implementer-2", "pending")

    # Attribution is part of the event, not of its resolution: closing the
    # ledger row must not lose who opened it.
    record_outcome("run-1", True, db_path=db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT lane, node_id, outcome FROM semantic_memory_uses",
        ).fetchone()
    finally:
        conn.close()
    assert row == ("frontier", "implementer-2", "win")


def test_attribution_defaults_to_unknown_for_existing_callers(db_path):
    """``lane``/``node_id`` are optional, so every caller written before this
    tranche keeps working untouched. An unattributed retrieval is recorded as
    unknown rather than guessed at — the same fails-closed posture as an
    unresolved outcome: a retrieval whose decision is not known cannot be
    credited to one."""
    emb = _FixedEmbedder({"q": [1.0, 0.0], "m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    # The pre-0058 call shape, verbatim: no lane, no node_id.
    assert record_retrievals(
        [mid], scope="s", run_id="run-1", task_class="code-fix", db_path=db_path,
    ) == 1

    conn = sqlite3.connect(str(db_path))
    try:
        lane, node_id = conn.execute(
            "SELECT lane, node_id FROM semantic_memory_uses",
        ).fetchone()
    finally:
        conn.close()
    assert (lane, node_id) == ("", ""), "unknown must not be invented"


def test_attribution_is_accounting_and_does_not_move_the_ranking(db_path):
    """The columns exist to make spend attributable, not to influence
    retrieval. Two memories with identical similarity and identical counters
    must rank identically whatever lane retrieved them — if attribution leaked
    into the score, a lane could launder rank by choosing its own bookkeeping."""
    lane_a = _seed(db_path, "s", "a", [1.0, 0.0])
    lane_b = _seed(db_path, "s", "b", [1.0, 0.0])
    for i in range(3):
        record_retrievals(
            [lane_a], scope="s", run_id=f"a{i}", lane="cheap",
            node_id="n", db_path=db_path,
        )
        record_retrievals(
            [lane_b], scope="s", run_id=f"b{i}", lane="frontier",
            node_id="n", db_path=db_path,
        )
        record_outcome(f"a{i}", True, db_path=db_path)
        record_outcome(f"b{i}", True, db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = dict(conn.execute(
            "SELECT lane, COUNT(*) FROM semantic_memory_uses GROUP BY lane",
        ).fetchall())
    finally:
        conn.close()
    assert rows == {"cheap": 3, "frontier": 3}, "spend is attributable per lane"

    emb = _FixedEmbedder({"q": [1.0, 0.0]})
    out = search("q", scope="s", top_k=2, db_path=db_path, embedder=emb)
    assert len(out) == 2
    assert out[0]["utility"] == out[1]["utility"] == pytest.approx(4 / 5)


# ── upsert(): mirror-a-source-table identity ─────────────────────────────────


def _seed_traces(db_path, rows) -> None:
    """Minimal ``execution_traces`` modelled on 0054, carrying only the three
    columns ``resolve_finished_runs`` reads. The real table is created by a
    migration this fixture does not run, so the sweep's JOIN needs a stand-in.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS execution_traces ("
            "  trace_id TEXT PRIMARY KEY,"
            "  run_id TEXT,"
            "  status TEXT NOT NULL"
            ")"
        )
        conn.executemany(
            "INSERT INTO execution_traces(trace_id, run_id, status) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_upsert_keys_identity_not_similarity(db_path):
    """A mirror of a source table needs one memory per source row. `add()`
    cannot give that: it reconciles by similarity, so two rows that read alike
    collapse into one. `upsert()` keys on the caller's key instead — which is
    the only thing that can tell two look-alike source rows apart."""
    from mini_ork.memory import upsert

    emb = _FixedEmbedder({"first": [1.0, 0.0], "second": [1.0, 0.0]})
    a = upsert("first", scope="s", key="row-1", db_path=db_path, embedder=emb)
    b = upsert("second", scope="s", key="row-2", db_path=db_path, embedder=emb)

    assert a != b, "distinct keys must be distinct memories"
    assert len(_rows(db_path, "s")) == 2, (
        "identical embeddings collapsed two keyed rows — identity leaked back "
        "into similarity"
    )


def test_upsert_refreshes_text_and_preserves_the_track_record(db_path):
    """Re-syncing a source row must not reset what the memory has earned. A
    refresh that zeroed `uses`/`wins` would launder a bad track record and
    hand every memory a fresh exploration bonus on every sync."""
    from mini_ork.memory import upsert

    emb = _FixedEmbedder({"old text": [1.0, 0.0], "new text": [1.0, 0.0]})
    mid = upsert("old text", scope="s", key="row-1", db_path=db_path, embedder=emb)
    record_retrievals([mid], scope="s", run_id="r1", db_path=db_path)
    record_outcome("r1", True, db_path=db_path)
    assert _by_text(db_path, "s")["old text"][1:] == (1, 1)

    again = upsert("new text", scope="s", key="row-1", db_path=db_path, embedder=emb)

    assert again == mid, "the same key must address the same row"
    rows = _rows(db_path, "s")
    assert len(rows) == 1, "a re-upsert must not insert a second row"
    assert rows[0][1] == "new text", "text should refresh"
    assert rows[0][2:] == (1, 1), "uses/wins must survive the refresh"


def test_upsert_scopes_the_same_key_independently(db_path):
    """Two task classes can hold a row with the same key without colliding."""
    from mini_ork.memory import upsert

    emb = _FixedEmbedder({"x": [1.0, 0.0]})
    one = upsert("x", scope="one", key="k", db_path=db_path, embedder=emb)
    two = upsert("x", scope="two", key="k", db_path=db_path, embedder=emb)
    assert one != two
    assert len(_rows(db_path, "one")) == 1 and len(_rows(db_path, "two")) == 1


# ── resolve_finished_runs(): the sweep that closes the loop ─────────────────


def test_resolve_finished_runs_sweeps_only_terminal_runs(db_path):
    """Three runs retrieved the same memory. Sweeping must resolve the one that
    finished, leave the one still executing alone, and leave the one with no
    traces at all pending — nothing was recorded to judge it by."""
    from mini_ork.memory import resolve_finished_runs

    emb = _FixedEmbedder({"m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    for run in ("done", "live", "absent"):
        record_retrievals([mid], scope="s", run_id=run, db_path=db_path)
    _seed_traces(db_path, [
        ("t1", "done", "success"),
        ("t2", "done", "success"),
        ("t3", "live", "success"),
        ("t4", "live", "running"),
    ])

    assert resolve_finished_runs(db_path=db_path) == 1

    conn = sqlite3.connect(str(db_path))
    try:
        outcomes = dict(conn.execute(
            "SELECT run_id, outcome FROM semantic_memory_uses",
        ).fetchall())
    finally:
        conn.close()
    assert outcomes == {"done": "win", "live": "pending", "absent": "pending"}
    assert _by_text(db_path, "s")["m"][1:] == (3, 1), "one win, on the ledger"


def test_resolve_finished_runs_marks_a_failed_run_as_loss(db_path):
    """The rule is `prior_runs_md`'s: a run is clean iff no node is anything
    but success-or-running. Same tables, same verdict — the memory cannot be
    credited by a run the prompt block would call failed."""
    from mini_ork.memory import resolve_finished_runs

    emb = _FixedEmbedder({"m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]

    record_retrievals([mid], scope="s", run_id="bad", db_path=db_path)
    _seed_traces(db_path, [("t1", "bad", "success"), ("t2", "bad", "failure")])

    assert resolve_finished_runs(db_path=db_path) == 1
    assert _by_text(db_path, "s")["m"][1:] == (1, 0), "a loss must not bump wins"

    conn = sqlite3.connect(str(db_path))
    try:
        outcome = conn.execute(
            "SELECT outcome FROM semantic_memory_uses WHERE run_id = 'bad'",
        ).fetchone()[0]
    finally:
        conn.close()
    assert outcome == "loss"


def test_resolve_finished_runs_is_idempotent(db_path):
    from mini_ork.memory import resolve_finished_runs

    emb = _FixedEmbedder({"m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]
    record_retrievals([mid], scope="s", run_id="done", db_path=db_path)
    _seed_traces(db_path, [("t1", "done", "success")])

    assert resolve_finished_runs(db_path=db_path) == 1
    assert resolve_finished_runs(db_path=db_path) == 0
    assert _by_text(db_path, "s")["m"][1:] == (1, 1), "wins must not double-count"


def test_resolve_finished_runs_is_cold_safe_without_the_traces_table(db_path):
    """A DB that never ran the migration creating `execution_traces` must not
    raise on the prompt-injection path — there is simply nothing to resolve."""
    from mini_ork.memory import resolve_finished_runs

    emb = _FixedEmbedder({"m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]
    record_retrievals([mid], scope="s", run_id="r", db_path=db_path)

    assert resolve_finished_runs(db_path=db_path) == 0
    assert _by_text(db_path, "s")["m"][1:] == (1, 0), "left pending, not guessed"


def test_resolve_finished_runs_ignores_retrievals_without_a_run_id(db_path):
    """An unattributable retrieval has no run to look up, so it stays pending
    forever. That is the fail-closed rule, restated at the sweep boundary."""
    from mini_ork.memory import resolve_finished_runs

    emb = _FixedEmbedder({"m": [1.0, 0.0]})
    add("m", scope="s", infer=False, db_path=db_path, embedder=emb)
    mid = _by_text(db_path, "s")["m"][0]
    record_retrievals([mid], scope="s", db_path=db_path)  # run_id=''
    _seed_traces(db_path, [("t1", "", "success")])

    assert resolve_finished_runs(db_path=db_path) == 0
    assert _by_text(db_path, "s")["m"][1:] == (1, 0)


# ── rank_with_prior: an external prior gates, utility reorders in the gate ───


def test_rank_with_prior_cold_reproduces_the_prior_order(db_path):
    """With nothing retrieved anywhere, exploration is zero and every utility
    is the neutral 0.5, so the composite is the normalised prior and nothing
    else. A scope nobody has tried ranks exactly as its prior says."""
    strong = _seed(db_path, "s", "strong", [1.0, 0.0])
    mid = _seed(db_path, "s", "mid", [1.0, 0.0])
    weak = _seed(db_path, "s", "weak", [1.0, 0.0])
    candidates = [(strong, 3.0), (mid, 2.0), (weak, 1.0)]

    out = rank_with_prior(candidates, scope="s", top_k=3, db_path=db_path)

    assert [h["text"] for h in out] == ["strong", "mid", "weak"]
    assert [h["prior"] for h in out] == [3.0, 2.0, 1.0]
    assert [h["utility"] for h in out] == [pytest.approx(0.5)] * 3
    # Normalised over the pool: the top prior is 1.0, the bottom 0.0. The cold
    # score is that normalised prior, with no bonus to move anything.
    assert [h["score"] for h in out] == pytest.approx([1.0, 0.5, 0.0])


def test_rank_with_prior_reorders_by_record_inside_equal_priors(db_path):
    """Equal priors leave no order for the gate to preserve, so the record is
    the whole ranking — the one case where utility alone decides."""
    a = _seed(db_path, "s", "alpha", [1.0, 0.0], uses=3, wins=3)
    b = _seed(db_path, "s", "beta", [1.0, 0.0])
    c = _seed(db_path, "s", "gamma", [1.0, 0.0], uses=4, wins=0)

    out = rank_with_prior(
        [(a, 7.0), (b, 7.0), (c, 7.0)], scope="s", top_k=3, db_path=db_path,
    )

    assert [h["text"] for h in out] == ["alpha", "beta", "gamma"]
    assert out[0]["utility"] == pytest.approx(4 / 5)   # (3+1)/(3+2)
    assert out[1]["utility"] == pytest.approx(0.5)     # untried → neutral
    assert out[2]["utility"] == pytest.approx(1 / 6)   # (0+1)/(4+2)


def test_rank_with_prior_exploration_outranks_a_break_even_record(db_path):
    """A memory retrieved 20 times and helped exactly half is neither proven
    nor discredited — its utility is dead neutral and all it carries is the
    small bonus for having been tried. An untried peer with the same prior
    outranks it, because sampling what nobody has tried is the only way any
    memory ever acquires a record."""
    tried = _seed(db_path, "s", "tried", [1.0, 0.0], uses=20, wins=10)
    untried = _seed(db_path, "s", "untried", [1.0, 0.0])

    out = rank_with_prior(
        [(tried, 2.0), (untried, 2.0)], scope="s", top_k=2, db_path=db_path,
    )

    assert out[0]["utility"] == pytest.approx(0.5), "the control: break-even"
    assert [h["text"] for h in out] == ["untried", "tried"]
    assert out[0]["score"] > out[1]["score"]


def test_rank_with_prior_gate_bounds_a_perfect_record(db_path):
    """Utility may only reorder what the prior admitted. A perfect record far
    down the prior order cannot buy its way into a small top_k — the gate is
    what stands between a measured ranking and one lucky streak."""
    ids = {
        name: _seed(db_path, "s", name, [1.0, 0.0])
        for name in ("p6", "p5", "p4", "p3", "p2", "p1")
    }
    candidates = [(ids[n], float(n[1:])) for n in ids]   # priors 6.0 … 1.0
    # Touch the weakest one so it has wins on the books.
    record_retrievals([ids["p1"]], scope="s", run_id="r", db_path=db_path)
    record_outcome("r", True, db_path=db_path)

    out = rank_with_prior(candidates, scope="s", top_k=1, db_path=db_path)

    assert len(out) == 1
    assert out[0]["text"] == "p6", "the gate, not the record, chose the pool"


def test_rank_with_prior_drops_candidates_it_cannot_vouch_for(db_path):
    """A prior is a claim about a memory in *this* scope. One pointing at a
    different scope's row, or at no row at all, is a claim nothing supports —
    so it is dropped rather than ranked on a prior nothing can check."""
    mine = _seed(db_path, "s", "mine", [1.0, 0.0])
    theirs = _seed(db_path, "other", "theirs", [1.0, 0.0])

    out = rank_with_prior(
        [(mine, 2.0), (theirs, 9.0), (999_999, 8.0)],
        scope="s", top_k=5, db_path=db_path,
    )

    assert [h["text"] for h in out] == ["mine"]


def test_rank_with_prior_rejects_a_scope_or_top_k_it_cannot_honour(db_path):
    """Both are caller errors with no sensible default: an empty scope would
    silently rank across every scope, and a non-positive top_k asks for a
    result set that cannot exist."""
    with pytest.raises(ValueError):
        rank_with_prior([(1, 1.0)], scope="", top_k=1, db_path=db_path)
    with pytest.raises(ValueError):
        rank_with_prior([(1, 1.0)], scope="s", top_k=0, db_path=db_path)
