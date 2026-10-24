"""Per-item utility for the v2 pack: resolve_finished, score, rerank, build wiring.

Hermetic: every test builds its own temp sqlite DB and temp MINI_ORK_HOME (the
ledger shape is ``runs/<run_id>/learned/<node>.json``); no network, no provider
credentials and no dependence on the operator's real state.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import mini_ork.context_v2 as context_v2
import mini_ork.learning.item_utility as item_utility
from mini_ork.memory import semantic

MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"

TASK_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
  id               TEXT PRIMARY KEY,
  task_class       TEXT NOT NULL,
  kickoff_path     TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'classified',
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
"""

KICKOFF = """# Item utility fixture

## Files in scope

- `src/widget.py`

## Goal

Order the pack by measured utility.
"""


def _make_db(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT)")
        con.executescript(TASK_RUNS_DDL)
        for name in ("0063_lesson_themes.sql", "0064_code_findings.sql"):
            con.executescript((MIGRATIONS / name).read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()
    return str(path)


def _add_run(db: str, run_id: str, status: str, created_at: int = 1) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, task_class, kickoff_path, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (run_id, "framework_edit", "/tmp/fixture.md", status,
             created_at, created_at))
        con.commit()
    finally:
        con.close()


def _write_learned(home: str, run_id: str, node: str, ids: list[str], *,
                   held_out: bool = False) -> None:
    learned = Path(home) / "runs" / run_id / "learned"
    learned.mkdir(parents=True, exist_ok=True)
    record = {
        "node_id": node,
        "held_out": held_out,
        "sources": [{"id": i, "kind": "context_v2"} for i in ids],
    }
    (learned / f"{node}.json").write_text(json.dumps(record), encoding="utf-8")


def _stats(db: str) -> dict[str, tuple[int, int]]:
    con = sqlite3.connect(db)
    try:
        return {
            str(row[0]): (int(row[1]), int(row[2]))
            for row in con.execute(
                "SELECT item_id, uses, wins FROM context_item_utility")
        }
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def _marked(db: str) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {str(r[0]) for r in con.execute(
            "SELECT run_id FROM context_item_runs")}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


def _seed_utility(db: str, item_id: str, uses: int, wins: int) -> None:
    con = sqlite3.connect(db)
    try:
        item_utility._ensure_tables(con)  # noqa: SLF001
        con.execute(
            "INSERT OR REPLACE INTO context_item_utility"
            " (item_id, uses, wins, last_used) VALUES (?,?,?,?)",
            (item_id, uses, wins, 1))
        con.commit()
    finally:
        con.close()


def _ids(items: list[dict]) -> list[str]:
    return [i["id"] for i in items]


# ── enabled ─────────────────────────────────────────────────────────────────

def test_enabled_defaults_on_and_only_zero_opts_out(monkeypatch):
    monkeypatch.delenv("MO_ITEM_UCB", raising=False)
    assert item_utility.enabled() is True
    monkeypatch.setenv("MO_ITEM_UCB", "false")
    assert item_utility.enabled() is True
    monkeypatch.setenv("MO_ITEM_UCB", "0")
    assert item_utility.enabled() is False


# ── score ───────────────────────────────────────────────────────────────────

def test_score_matches_semantic_formula():
    for sim in (0.0, 0.37, 1.0):
        for uses, wins in ((0, 0), (10, 0), (10, 10), (40, 3)):
            for n_total in (0, 1, 80, 1000):
                expected = (
                    sim
                    + semantic.W_UTILITY
                    * (semantic._utility(uses, wins) - semantic.UTILITY_NEUTRAL)
                    + semantic.W_EXPLORE
                    * semantic._exploration(uses, n_total)
                )
                assert item_utility.score(
                    sim, uses, wins, n_total) == pytest.approx(expected)


def test_constants_are_imported_not_copied():
    for name in ("W_UTILITY", "W_EXPLORE", "UTILITY_NEUTRAL",
                 "_utility", "_exploration"):
        assert getattr(item_utility, name) is getattr(semantic, name)


# ── resolve_finished ────────────────────────────────────────────────────────

def test_resolve_counts_terminal_runs_once(tmp_path):
    db = _make_db(tmp_path / "state.db")
    home = str(tmp_path / "home")
    Path(home).mkdir()

    _add_run(db, "run1", "published", 1)
    _write_learned(home, "run1", "planner", ["t:a", "f:x"])
    _write_learned(home, "run1", "reviewer", ["t:a", "t:c"])  # t:a once
    _add_run(db, "run2", "failed", 2)
    _write_learned(home, "run2", "planner", ["t:a"])
    _add_run(db, "run3", "published", 3)
    _write_learned(home, "run3", "planner", ["t:b"], held_out=True)

    assert item_utility.resolve_finished(home, db) == 2
    stats = _stats(db)
    assert stats["t:a"] == (2, 1)
    assert stats["f:x"] == (1, 1)
    assert stats["t:c"] == (1, 1)
    assert "t:b" not in stats
    # The all-held-out run is not counted, but it is marked so it stops
    # occupying the limit window.
    assert {"run1", "run2", "run3"} <= _marked(db)

    # Every terminal run is now marked, so a second pass counts nothing.
    assert item_utility.resolve_finished(home, db) == 0


def test_resolve_marks_held_out_runs_so_a_full_window_does_not_wedge(tmp_path):
    db = _make_db(tmp_path / "state.db")
    home = str(tmp_path / "home")
    Path(home).mkdir()

    for n in range(3):
        # Holdouts are the newest runs: resolve_finished walks newest first, so
        # they are what a narrow window reaches before the evidence-bearing run.
        _add_run(db, f"hold{n}", "published", 100 + n)
        _write_learned(home, f"hold{n}", "planner", ["t:h"], held_out=True)
    _add_run(db, "real", "published", 1)
    _write_learned(home, "real", "planner", ["t:r"])

    # A window narrower than the holdout backlog: the first pass spends it
    # entirely on holdout runs, which must still be marked.
    assert item_utility.resolve_finished(home, db, limit=3) == 0
    assert {"hold0", "hold1", "hold2"} <= _marked(db)
    assert _stats(db) == {}

    # The next pass therefore reaches the evidence-bearing run.
    assert item_utility.resolve_finished(home, db, limit=3) == 1
    assert _stats(db)["t:r"] == (1, 1)
    assert "t:h" not in _stats(db)


def test_resolve_skips_executing_runs(tmp_path):
    db = _make_db(tmp_path / "state.db")
    home = str(tmp_path / "home")
    Path(home).mkdir()
    _add_run(db, "run-exec", "executing", 1)
    _write_learned(home, "run-exec", "planner", ["t:a"])

    assert item_utility.resolve_finished(home, db) == 0
    assert _stats(db) == {}
    assert "run-exec" not in _marked(db)


def test_resolve_marks_a_run_with_no_learned_dir(tmp_path):
    db = _make_db(tmp_path / "state.db")
    home = str(tmp_path / "home")
    (Path(home) / "runs" / "bare").mkdir(parents=True)
    _add_run(db, "bare", "published", 1)

    assert item_utility.resolve_finished(home, db) == 1
    assert _stats(db) == {}
    assert "bare" in _marked(db)


def test_resolve_skips_a_corrupt_learned_file_but_counts_the_rest(tmp_path):
    db = _make_db(tmp_path / "state.db")
    home = str(tmp_path / "home")
    _add_run(db, "run-corrupt", "published", 1)
    _write_learned(home, "run-corrupt", "planner", ["t:ok"])
    learned = Path(home) / "runs" / "run-corrupt" / "learned"
    (learned / "reviewer.json").write_text("{ not json", encoding="utf-8")

    assert item_utility.resolve_finished(home, db) == 1
    assert _stats(db)["t:ok"] == (1, 1)


def test_resolve_swallows_a_broken_db_path(tmp_path):
    broken = str(tmp_path / "no" / "such" / "dir" / "state.db")
    assert item_utility.resolve_finished(str(tmp_path), broken) == 0


# ── rerank ──────────────────────────────────────────────────────────────────

def test_rerank_without_stats_keeps_incoming_order(tmp_path):
    db = str(tmp_path / "fresh.db")
    items = [{"id": f"t:{i}", "text": f"lesson {i}"} for i in range(6)]
    assert _ids(item_utility.rerank(items, db=db)) == _ids(items)


def test_rerank_wins_overtake_losses(tmp_path):
    db = str(tmp_path / "state.db")
    _seed_utility(db, "t:losses", 10, 0)
    _seed_utility(db, "t:wins", 10, 10)
    items = [{"id": "t:losses"}, {"id": "t:wins"}, {"id": "t:c"},
             {"id": "t:d"}, {"id": "t:e"}]

    out = _ids(item_utility.rerank(items, db=db))
    assert out.index("t:wins") < out.index("t:losses")
    assert set(out) == {i["id"] for i in items}


def test_rerank_unused_item_gets_exploration_bonus(tmp_path):
    db = str(tmp_path / "state.db")
    _seed_utility(db, "t:lossy", 20, 0)
    items = [{"id": "t:lossy", "s": 0.9}, {"id": "t:fresh", "s": 0.9}]

    assert _ids(item_utility.rerank(items, db=db, similarity_key="s")) == [
        "t:fresh", "t:lossy"]
    assert item_utility.score(0.9, 0, 0, 20) > item_utility.score(0.9, 20, 0, 20)


def test_rerank_is_stable_for_ties(tmp_path):
    db = str(tmp_path / "state.db")
    _seed_utility(db, "t:x", 5, 5)
    _seed_utility(db, "t:y", 5, 5)
    items = [{"id": "t:x", "s": 0.7}, {"id": "t:y", "s": 0.7},
             {"id": "t:z", "s": 0.2}]
    assert _ids(item_utility.rerank(items, db=db, similarity_key="s"))[:2] == [
        "t:x", "t:y"]


def test_rerank_swallows_a_broken_db_path(tmp_path):
    items = [{"id": "t:a"}, {"id": "t:b"}]
    broken = str(tmp_path / "no" / "such" / "dir" / "state.db")
    assert _ids(item_utility.rerank(items, db=broken)) == ["t:a", "t:b"]


# ── build wiring ────────────────────────────────────────────────────────────

def _seed_pack_fixture(db: str) -> None:
    con = sqlite3.connect(db)
    try:
        ts = int(time.time())
        for i, issue in enumerate((
                "serializer truncates payload bytes whilst encoding",
                "serializer truncates payload bytes during encoding",)):
            con.execute(
                "INSERT INTO code_findings (fingerprint, run_id, source, file,"
                " line, severity, category, issue, snippet, ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"fp-{i}", f"fx-{i}", "review:reviewer", "src/widget.py",
                 1, "high", "correctness", issue, None, ts))
        lesson_texts = {
            "T1": "prefer narrow worktree passes for runtime surfaces",
            "T2": "gate expensive lenses behind explicit spend caps",
        }
        for rank, (theme_id, n_runs) in enumerate((("T1", 60), ("T2", 59))):
            con.execute(
                "INSERT INTO lesson_themes (theme_id, kind, task_class,"
                " representative, centroid, n_runs, first_seen, last_seen,"
                " lesson_text, status, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (theme_id, "task", "framework_edit", lesson_texts[theme_id],
                 b"fx", n_runs, 1, 100 - rank, lesson_texts[theme_id],
                 "verified", 1))
        con.commit()
    finally:
        con.close()


def test_build_ucb_off_is_prior_order(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "state.db")
    _seed_pack_fixture(db)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MO_FINDINGS_LANDING", "0")
    monkeypatch.setenv("MO_FINDINGS_STALENESS", "0")
    monkeypatch.setenv("MO_CONTEXT_V2_MAX_AGE_DAYS", "0")
    monkeypatch.setenv("MO_ITEM_UCB", "0")
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)

    pack = context_v2.build(
        "/tmp/item-utility-fixture.md", task_class="framework_edit", db=db,
        run_id="fixture-run", kickoff_text=KICKOFF)

    assert pack["item_ranking"] == "prior-order"
    # Prior order is the declared pre-change rule: themes by n_runs DESC.
    assert _ids(pack["themes"]) == ["t:T1", "t:T2"]
    # Clusters keep the cluster() order unchanged.
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        findings = [dict(r) for r in con.execute(
            "SELECT * FROM code_findings ORDER BY id")]
    finally:
        con.close()
    expected = [c["id"] for c in context_v2.cluster(findings)]
    assert _ids(pack["file_findings"]) == expected
