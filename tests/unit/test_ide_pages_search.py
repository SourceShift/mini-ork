"""``board runs --query`` — the FTS5 search index + LIKE fallback."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import search as search_mod
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


def _seed_run(home: Path, run_id: str, *, recipe: str = "code-fix",
              status: str = "published", created_at: int) -> Path:
    """Insert one ``task_runs`` row + matching run dir + a per-run kickoff.

    Returns the run dir so the caller can drop extra artifacts
    (``verdict.json``, ``lens-*.md``, …) into it.
    """
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "task_class, kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, status, 0.0, created_at, created_at,
         "code_fix", "", "latest"),
    )
    con.commit()
    con.close()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    kickoff = home / "kickoffs" / f"{run_id}.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text(f"# Run {run_id}\n\nDistinct feature {run_id}.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET kickoff_path = ? WHERE id = ?",
                (str(kickoff), run_id))
    con.commit()
    con.close()
    return run_dir


def _make_runs(home: Path, n: int) -> list[str]:
    """Seed ``n`` runs at evenly-spaced timestamps for deterministic paging."""
    base = 1_791_000_000
    rids: list[str] = []
    for i in range(n):
        rid = f"run-{base + i:09d}-{('abcdef'[i % 6]) * 6}"
        _seed_run(home, rid, created_at=base + i * 60)
        rids.append(rid)
    return rids


def _seed_unique_word(home: Path, rid: str, word: str) -> None:
    """Append a distinct feature word to one run's own kickoff + bump mtime.

    Each run keeps its own ``kickoff<rid>.md`` so neighbouring runs' words
    don't collide. ``task_runs.kickoff_path`` is updated to point at the new
    file so the indexer reads the per-run text.
    """
    run_dir = home / "runs" / rid
    kickoff = home / "kickoffs" / f"{rid}.md"
    kickoff.write_text(f"# Run {rid}\n\nWord: {word}\n")
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET kickoff_path = ? WHERE id = ?",
                (str(kickoff), rid))
    con.commit()
    con.close()
    run_dir.touch()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _recorder_spawn(_args, *, log_handle=None):  # noqa: ARG001
    """No-op ``_spawn_indexer`` for unit tests: no real builder ever runs."""
    return type("Fake", (), {"pid": None})()


@pytest.fixture(autouse=True)
def _no_real_builder(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    """No real detached builder ever runs in unit tests (kickoff r5 §Tests).

    Pyright reports this fixture as unused because it doesn't appear in
    any test's parameter list, but pytest invokes it implicitly via the
    ``autouse=True`` flag — every test gets the recorder installed on
    ``_spawn_indexer``.
    """
    monkeypatch.setattr(search_mod, "_spawn_indexer", _recorder_spawn)
    # Tests that do not pass time_budget= must not depend on machine speed.
    monkeypatch.setattr(search_mod, "_REINDEX_TIME_BUDGET_S", 600.0)


# ── reindex ──────────────────────────────────────────────────────────────────


def test_reindex_walks_stale_runs_and_populates_the_fts_table(home: Path) -> None:
    _make_runs(home, 3)
    n = search_mod.reindex(home)
    assert n == 3
    fts = sqlite3.connect(home / "state" / "ide-search.sqlite")
    fts_row_count = fts.execute("SELECT COUNT(*) FROM runs_fts").fetchone()[0]
    fts.close()
    assert fts_row_count == 3


def test_reindex_skips_already_indexed_ones(home: Path) -> None:
    """A second reindex (after the first indexed everything) returns 0
    because nothing is stale — kickoff r5 dropped the warm short-circuit
    so every call runs the candidate pass, but no candidate is stale."""
    _make_runs(home, 2)
    search_mod.reindex(home)
    n2 = search_mod.reindex(home)
    assert n2 == 0  # nothing stale


def test_reindex_respects_max_runs_budget(home: Path) -> None:
    """``max_runs`` is an additional safety upper bound (kickoff r3 fix #2):
    a 1.2 s time budget is the primary constraint, but ``max_runs=2`` caps
    the per-call index even when the budget would allow more."""
    _make_runs(home, 5)
    n = search_mod.reindex(home, max_runs=2)
    assert n == 2
    fts = sqlite3.connect(home / "state" / "ide-search.sqlite")
    count = fts.execute("SELECT COUNT(*) FROM runs_fts").fetchone()[0]
    fts.close()
    assert count == 2


def test_reindex_picks_up_a_changed_kickoff(home: Path) -> None:
    """A kickoff edit between two ``reindex`` calls is picked up via the
    per-run staleness check (``updated_at != seen_updated_at``).

    In production the kickoff-edit control plane bumps ``updated_at``;
    this test mirrors that by bumping ``updated_at`` AND mutating the
    kickoff file. The bump alone is sufficient under r5 — no extra
    artifact drop required (r4 used ``_stat_mtime_for`` to catch edits
    that bumped ``updated_at`` without a file change; r5 uses the
    SQL candidate filter instead).
    """
    rids = _make_runs(home, 1)
    search_mod.reindex(home)
    rid = rids[0]
    _seed_unique_word(home, rid, "newword")
    # Kickoff-edit control plane always bumps ``updated_at`` (kickoff r3).
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET updated_at = updated_at + 1 WHERE id = ?",
                (rid,))
    con.commit()
    con.close()
    n = search_mod.reindex(home)
    assert n == 1
    run_ids, total = search_mod.search(home, "newword", limit=10, offset=0)
    assert run_ids == [rid]
    assert total == 1


def test_reindex_indexes_main_artifacts(home: Path) -> None:
    rid = "run-artifacts-123"
    run_dir = _seed_run(home, rid, created_at=1_791_000_000)
    (run_dir / "verdict.json").write_text('{"verdict": "verdictword pass"}')
    (run_dir / "lens-deep.md").write_text("# synthesis\n\nlensword analysis\n")
    (run_dir / "implementer-summary.json").write_text('{"summary": "summaryword"}')
    n = search_mod.reindex(home)
    assert n == 1
    for term in ("verdictword", "lensword", "summaryword"):
        run_ids, total = search_mod.search(home, term, limit=10, offset=0)
        assert run_ids == [rid], f"term {term!r} not found in artifacts"
        assert total == 1


# ── search ───────────────────────────────────────────────────────────────────


def test_search_ands_two_terms(home: Path) -> None:
    rids = _make_runs(home, 3)
    _seed_unique_word(home, rids[0], "apple banana")
    _seed_unique_word(home, rids[1], "apple cherry")
    _seed_unique_word(home, rids[2], "banana durian")
    search_mod.reindex(home)
    run_ids, total = search_mod.search(home, "apple banana", limit=10, offset=0)
    assert total == 1
    assert run_ids == [rids[0]]


def test_search_prefix_match(home: Path) -> None:
    rids = _make_runs(home, 2)
    _seed_unique_word(home, rids[0], "featurealpha")
    _seed_unique_word(home, rids[1], "featurebeta")
    search_mod.reindex(home)
    run_ids, total = search_mod.search(home, "feature", limit=10, offset=0)
    assert total == 2
    assert set(run_ids) == set(rids)


def test_search_pagination(home: Path) -> None:
    """Paging with offset/limit returns disjoint pages of the same total."""
    _make_runs(home, 12)
    search_mod.reindex(home)
    page1, total1 = search_mod.search(home, "run", limit=5, offset=0)
    page2, total2 = search_mod.search(home, "run", limit=5, offset=5)
    assert total1 == 12 and total2 == 12
    assert len(page1) == 5 and len(page2) == 5
    assert set(page1).isdisjoint(set(page2))


def test_search_empty_query_returns_zero(home: Path) -> None:
    _make_runs(home, 2)
    search_mod.reindex(home)
    run_ids, total = search_mod.search(home, "   ", limit=10, offset=0)
    assert run_ids == [] and total == 0


def test_search_special_chars_are_stripped(home: Path) -> None:
    """Parens and quotes do not raise — they are stripped / escaped."""
    _make_runs(home, 1)
    search_mod.reindex(home)
    # ``"run("`` strips to ``run`` — still a match.
    _run_ids, total = search_mod.search(home, '"run(', limit=10, offset=0)  # pyright: ignore[reportUnusedVariable]
    assert total >= 1


# ── like fallback ────────────────────────────────────────────────────────────


def test_like_fallback_when_fts5_is_unavailable(home: Path, monkeypatch) -> None:
    """If FTS5 raises on schema probe, search falls back to LIKE and reports
    the degraded state in ``errors``."""
    monkeypatch.setattr(search_mod, "_fts5_available", lambda *_args, **_kwargs: False)  # pyright: ignore[reportUnusedParameter]

    rids = _make_runs(home, 2)
    _seed_unique_word(home, rids[0], "fallback-only-word")
    errors: dict[str, str] = {}
    run_ids, total = search_mod.search(home, "fallback-only-word",
                                       limit=10, offset=0, errors=errors)
    assert total == 1
    assert run_ids == [rids[0]]
    assert "FTS5 unavailable" in errors["search"]


def test_no_state_db_is_an_empty_search(home: Path) -> None:
    (home / "state.db").unlink()
    run_ids, total = search_mod.search(home, "anything", limit=10, offset=0)
    assert run_ids == [] and total == 0


# ── kickoff ide-kickoff-search-r5 — per-run staleness, no window, no watermark ──


def test_reindex_on_warm_index_does_not_read_bodies(home: Path, monkeypatch) -> None:
    """After a first ``reindex`` warms the FTS table, a second ``reindex``
    must not call ``_body_for`` at all — staleness is decided by
    ``updated_at != seen_updated_at`` (a SQL dict compare), no per-run
    body read.

    r5 dropped the warm short-circuit; the invariant still holds because
    the second call has no stale candidates (every run's seen_updated_at
    already equals its updated_at).
    """
    _make_runs(home, 3)
    # First reindex — warms the index, may call _body_for.
    search_mod.reindex(home)

    body_calls: list[tuple] = []
    original_body_for = search_mod._body_for

    def spy_body_for(*args, **kwargs):
        body_calls.append((args, kwargs))
        return original_body_for(*args, **kwargs)

    monkeypatch.setattr(search_mod, "_body_for", spy_body_for)

    # Second reindex — nothing is stale, so _body_for must be untouched.
    n = search_mod.reindex(home)
    assert n == 0
    assert body_calls == [], f"_body_for called on warm index: {body_calls!r}"


def test_reindex_picks_up_run_whose_updated_at_advances(home: Path) -> None:
    """When a run's ``task_runs.updated_at`` moves past its
    ``seen_updated_at``, the next ``reindex`` picks it up via the
    per-run SQL candidate filter — no stat cascade, no watermark.

    The kickoff-edit control plane bumps ``updated_at``; this test mirrors
    that. No artifact drop required under r5 (the SQL filter alone
    detects the bump).
    """
    rids = _make_runs(home, 1)
    search_mod.reindex(home)
    rid = rids[0]

    # Bump updated_at well past the seed timestamp. No verdict.json
    # drop — r5's staleness is SQL-only.
    new_ts = 1_791_000_000 + 99_999
    run_dir = home / "runs" / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET updated_at = ? WHERE id = ?", (new_ts, rid))
    con.commit()
    con.close()

    n = search_mod.reindex(home)
    assert n == 1, f"expected run picked up by per-run staleness, got n={n}"


def test_reindex_time_budget_stops_early_and_reports_indexing(
    home: Path,
) -> None:
    """With a sub-millisecond ``time_budget``, the reindex loop indexes at
    least one run (the cold-start guarantee) then stops with a partial
    ``errors["index"] = "indexing: <done>/<total> runs"`` note that the
    payload layer surfaces as ``"indexing": True``."""
    _make_runs(home, 10)
    errors: dict[str, str] = {}
    n = search_mod.reindex(home, time_budget=1e-9, errors=errors)
    assert 1 <= n < 10, f"expected partial indexing 1..9, got n={n}"
    assert "index" in errors, "partial indexing must set errors['index']"
    note = errors["index"]
    assert note.startswith("indexing: "), note
    assert note.endswith(" runs"), note
    done_str, total_str = note.removeprefix("indexing: ").removesuffix(" runs").split("/")
    assert int(done_str) == n
    assert int(total_str) == 10


def test_reindex_reindexes_runs_after_cache_wipe(home: Path) -> None:
    """r5 §Tests replacement for ``test_reindex_reports_indexing_inside_warm_window``:
    with no warm-window short-circuit, wiping the cache between two calls
    rebuilds the index from scratch on the next call.

    Seed 5 runs → first reindex indexes all 5 → wipe ``runs_fts`` +
    ``indexed`` (simulates "user deleted the cache") → second reindex
    indexes all 5 again because every run is now never-indexed.
    """
    _make_runs(home, 5)
    n1 = search_mod.reindex(home)
    assert n1 == 5, f"first reindex should index all 5, got n={n1}"

    # Simulate user wiping the cache between calls.
    fts = sqlite3.connect(home / "state" / "ide-search.sqlite")
    fts.execute("DELETE FROM runs_fts")
    fts.execute("DELETE FROM indexed")
    fts.commit()
    fts.close()

    # Next call rebuilds the index — every run is never-indexed again,
    # so all 5 get indexed in one budgeted call.
    n2 = search_mod.reindex(home)
    assert n2 == 5, f"expected 5 runs reindexed after cache wipe, got n={n2}"

    fts = sqlite3.connect(home / "state" / "ide-search.sqlite")
    row_count = fts.execute("SELECT COUNT(*) FROM runs_fts").fetchone()[0]
    fts.close()
    assert row_count == 5


def test_reindex_picks_up_edit_during_a_backlog(home: Path) -> None:
    """r5 §Tests: an edit that lands while a backlog is being drained
    must still be picked up. 2 indexed runs + edit run[0] + seed 30
    new runs. Six ``reindex(time_budget=1e-9)`` calls each index 1
    run (cold-start guarantee), then ``reindex()`` drains the rest.
    ``search('zebrafish')`` must find run[0].
    """
    base = 1_791_000_000
    rids = _make_runs(home, 2)
    # First reindex indexes both — seen_updated_at == updated_at for both.
    search_mod.reindex(home)
    rid0 = rids[0]
    # Edit run[0]: add ``zebrafish`` to its kickoff + bump updated_at.
    _seed_unique_word(home, rid0, "zebrafish")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "UPDATE task_runs SET updated_at = updated_at + 1 WHERE id = ?",
        (rid0,),
    )
    con.commit()
    con.close()
    # Seed 30 new runs at base + 0..29*60 — they collide with the
    # original 2 on timestamp but the run ids differ. The original 2
    # are at base + 0 and base + 60; the 30 new are at base + 0..29*60.
    # ``_make_runs`` uses a deterministic id scheme so all 32 ids are
    # unique.
    for i in range(30):
        _seed_run(home, f"backlog-run-{i:09d}", created_at=base + i * 60)

    # Six budget-tight calls — each indexes exactly one run (cold start)
    # before the budget runs out.
    for _ in range(6):
        search_mod.reindex(home, time_budget=1e-9)

    # Default-budget reindex drains the rest.
    for _ in range(20):  # bounded safety loop
        errors: dict[str, str] = {}
        search_mod.reindex(home, errors=errors)
        if "index" not in errors:
            break
        note = errors["index"]
        done_str, total_str = (
            note.removeprefix("indexing: ").removesuffix(" runs").split("/")
        )
        if int(done_str) == int(total_str):
            break

    run_ids, total = search_mod.search(home, "zebrafish", limit=10, offset=0)
    assert run_ids == [rid0], f"edited run must be searchable: got {run_ids!r}"
    assert total == 1


def test_reindex_budget_with_slow_body_caps_home(home: Path, monkeypatch) -> None:
    """r5 §Tests: 40 never-indexed runs with ``_body_for`` monkeypatched
    to sleep 0.05 s and ``time_budget=0.12`` → 1-3 runs indexed,
    elapsed < 0.3 s, ``indexing`` flag set.

    The cold-start guarantee means one run always indexes; the rest are
    budgeted out.
    """
    _make_runs(home, 40)
    original_body = search_mod._body_for

    def slow_body(*args, **kwargs):
        time.sleep(0.05)
        return original_body(*args, **kwargs)

    monkeypatch.setattr(search_mod, "_body_for", slow_body)

    errors: dict[str, str] = {}
    t0 = time.monotonic()
    n = search_mod.reindex(home, time_budget=0.12, errors=errors)
    elapsed = time.monotonic() - t0

    assert 1 <= n <= 3, f"expected 1..3 runs indexed under tight budget, got n={n}"
    assert elapsed < 0.3, f"expected elapsed < 0.3 s, got {elapsed:.3f} s"
    assert "index" in errors, "partial indexing must set errors['index']"
    note = errors["index"]
    assert note.startswith("indexing: "), note
    done_str, total_str = (
        note.removeprefix("indexing: ").removesuffix(" runs").split("/")
    )
    assert int(done_str) == n
    assert int(total_str) == 40


def test_reindex_time_budget_covers_whole_call(home: Path) -> None:
    """The clock starts at the top of ``reindex`` (r5 §2). A near-zero
    budget short-circuits the index loop after the first candidate —
    measured wall time stays under the budget plus the first-candidate's
    body work.
    """
    _make_runs(home, 10)
    t0 = time.monotonic()
    errors: dict[str, str] = {}
    n = search_mod.reindex(home, time_budget=1e-9, errors=errors)
    elapsed = time.monotonic() - t0
    assert 1 <= n < 10
    # One candidate body read is unavoidable — that alone takes a few
    # ms. Beyond that the budget is enforced.
    assert elapsed < 1.0, f"call should finish fast under 1e-9 budget, took {elapsed:.3f}s"


# ── background builder (flock) ───────────────────────────────────────────────


def _spawn_recorder(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    def fake_spawn(args, *, log_handle):
        calls.append(tuple(args))
        return type("Fake", (), {"pid": None})()

    monkeypatch.setattr(search_mod, "_spawn_indexer", fake_spawn)
    return calls


def test_no_builder_spawns_while_one_holds_the_lock(home: Path, monkeypatch) -> None:
    calls = _spawn_recorder(monkeypatch)
    held = search_mod._try_lock(home)
    assert held is not None
    try:
        search_mod._maybe_spawn_builder(home)
        assert calls == []
    finally:
        held.close()
    search_mod._maybe_spawn_builder(home)
    assert len(calls) == 1 and calls[0][-1] == "--build"


def test_a_second_builder_exits_at_once(home: Path) -> None:
    _make_runs(home, 3)
    held = search_mod._try_lock(home)
    try:
        assert search_mod._run_build(home) == 0
    finally:
        held.close()


def test_reindex_spawns_the_builder_only_when_work_remains(home: Path, monkeypatch) -> None:
    calls = _spawn_recorder(monkeypatch)
    _make_runs(home, 6)
    errors: dict[str, str] = {}
    search_mod.reindex(home, max_runs=2, errors=errors)
    assert len(calls) == 1 and errors["index"] == "indexing: 2/6 runs"
    errors = {}
    search_mod.reindex(home, errors=errors)
    assert errors == {} and len(calls) == 1


def test_build_indexes_everything_and_releases_the_lock(home: Path, monkeypatch) -> None:
    _make_runs(home, 7)
    monkeypatch.setattr(search_mod, "_BUILD_SLEEP_S", 0)
    assert search_mod._run_build(home) == 7
    fts = sqlite3.connect(home / "state" / "ide-search.sqlite")
    assert fts.execute("SELECT COUNT(*) FROM runs_fts").fetchone()[0] == 7
    assert fts.execute("SELECT COUNT(*) FROM indexed").fetchone()[0] == 7
    fts.close()
    lock = search_mod._try_lock(home)
    assert lock is not None, "the builder must release its lock on exit"
    lock.close()


def test_build_commits_once_per_batch(home: Path, monkeypatch) -> None:
    _make_runs(home, 7)
    monkeypatch.setattr(search_mod, "_BUILD_BATCH", 3)
    monkeypatch.setattr(search_mod, "_BUILD_SLEEP_S", 0)
    real_connect = search_mod._connect
    batch_commits = [0]

    def counting_connect(home_arg):
        con = real_connect(home_arg)

        class Counting:
            def __getattr__(self, name):
                return getattr(con, name)

            def commit(self):
                if con.in_transaction and con.total_changes:
                    batch_commits[0] += 1
                return con.commit()

        return Counting()

    monkeypatch.setattr(search_mod, "_connect", counting_connect)
    assert search_mod._run_build(home) == 7
    # Batches of 3, 3 and 1 (the schema commit has no pending transaction).
    assert batch_commits[0] == 3


def test_build_refreshes_stale_runs_too(home: Path, monkeypatch) -> None:
    rids = _make_runs(home, 3)
    monkeypatch.setattr(search_mod, "_BUILD_SLEEP_S", 0)
    search_mod.reindex(home)
    run_dir = home / "runs" / rids[0]
    (run_dir / "kickoff.md").write_text("# zebrafish refresh\n")
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET updated_at = updated_at + 100, kickoff_path = ? WHERE id = ?",
                (str(run_dir / "kickoff.md"), rids[0]))
    con.commit()
    con.close()
    assert search_mod._run_build(home) == 1
    assert search_mod.search(home, "zebrafish", 10, 0)[0] == [rids[0]]


def test_warm_home_indexes_new_runs_on_the_next_call(home: Path) -> None:
    _make_runs(home, 3)
    search_mod.reindex(home)
    for i in range(5):
        _seed_run(home, f"run-1792000000-new{i:03d}", created_at=1_792_000_000 + i)
    errors: dict[str, str] = {}
    assert search_mod.reindex(home, errors=errors) == 5
    assert errors == {}


def test_ensure_schema_drops_indexed_when_seen_updated_at_missing(
    home: Path,
) -> None:
    """r5 §Tests migration: a pre-r5 ``indexed`` table (with ``mtime``
    instead of ``seen_updated_at``) is dropped + recreated on the next
    ``_ensure_schema`` call. The index is a derived cache so losing
    the rows is acceptable (``reindex`` repopulates).
    """
    _make_runs(home, 2)
    # First call creates the r5 schema.
    search_mod.reindex(home)
    # Manually rewrite ``indexed`` to the pre-r5 schema.
    fts = sqlite3.connect(home / "state" / "ide-search.sqlite")
    fts.execute("DROP TABLE indexed")
    fts.execute("CREATE TABLE indexed(run_id PRIMARY KEY, mtime REAL)")
    fts.execute(
        "INSERT INTO indexed(run_id, mtime) VALUES (?, ?)",
        ("legacy-run", 0.0),
    )
    fts.commit()
    fts.close()

    # Next call's ``_ensure_schema`` must drop + recreate.
    search_mod._ensure_schema(
        sqlite3.connect(home / "state" / "ide-search.sqlite")
    )
    fts = sqlite3.connect(home / "state" / "ide-search.sqlite")
    cols = fts.execute("PRAGMA table_info(indexed)").fetchall()
    fts.close()
    col_names = {row[1] for row in cols}
    assert "seen_updated_at" in col_names, (
        f"indexed must have seen_updated_at after migration, got cols={col_names}"
    )
    assert "mtime" not in col_names, (
        f"pre-r5 mtime column must be gone, got cols={col_names}"
    )

def test_a_query_only_reports_progress_while_the_builder_runs(home: Path, monkeypatch) -> None:
    calls = _spawn_recorder(monkeypatch)
    _make_runs(home, 4)
    held = search_mod._try_lock(home)
    try:
        errors: dict[str, str] = {}
        assert search_mod.reindex(home, errors=errors) == 0
        assert errors["index"] == "indexing: 0/4 runs" and calls == []
    finally:
        held.close()
