"""Hermetic tests for ``mini_ork.learning.ledger``.

Models after ``test_cost_ledger.py`` (lightweight: temp sqlite, no migrations,
only the columns the API touches). Covers the kickoff's contract list:

- record_injections dedupes via the unique key + skips no-id sources.
- injection_counts aggregates correctly and respects ``since``.
- stage_health alarm logic: outputs==0 in ALL window rows + inputs>0 in at
  least one -> alarm; one non-zero output -> no alarm; inputs==0 -> no alarm.
- _write_learned_record writes ledger rows when block is non-empty; writes
  none when empty; swallows DB errors (Files still written, nothing raises).
- A bare DB without the migration works because ensure_schema runs first.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.learning import ledger  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────────


def _bare_db(path: Path) -> Path:
    """An empty sqlite. ensure_schema() must build the tables on demand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.close()
    return path


# ── record_injections ──────────────────────────────────────────────────────────


def test_record_injections_inserts_one_row_per_source(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    sources = [
        {"kind": "gradient", "id": "g1", "text": "x"},
        {"kind": "pattern", "id": "p1", "text": "y"},
        {"kind": "steering", "id": "s1", "text": "z"},
    ]
    n = ledger.record_injections(
        "run-1", "node-1", "researcher", "codex_lens", "framework_edit",
        attempt=1, sources=sources, db=str(db),
    )
    assert n == 3

    con = sqlite3.connect(db)
    try:
        rows = list(con.execute(
            "SELECT run_id, node_id, source_kind, source_id FROM lesson_injections"
        ))
    finally:
        con.close()
    assert len(rows) == 3
    kinds = sorted(r[2] for r in rows)
    assert kinds == ["gradient", "pattern", "steering"]


def test_record_injections_dedupes_on_identical_call(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    sources = [{"kind": "gradient", "id": "g1"}]
    n1 = ledger.record_injections(
        "run-1", "node-1", "researcher", "lane", "tc",
        attempt=1, sources=sources, db=str(db),
    )
    n2 = ledger.record_injections(
        "run-1", "node-1", "researcher", "lane", "tc",
        attempt=1, sources=sources, db=str(db),
    )
    assert n1 == 1
    assert n2 == 0


def test_record_injections_skips_source_without_id(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    sources = [
        {"kind": "gradient", "id": "g1"},
        {"kind": "gradient", "text": "orphan"},
        {"id": "g2"},            # no kind
        "not-a-dict",            # not a dict
    ]
    n = ledger.record_injections(
        "run-1", "node-1", "researcher", "lane", "tc",
        attempt=1, sources=sources, db=str(db),
    )
    assert n == 1


def test_record_injections_silent_on_missing_db(tmp_path: Path) -> None:
    n = ledger.record_injections(
        "run-1", "node-1", None, None, None,
        attempt=1, sources=[{"kind": "gradient", "id": "g1"}],
        db=str(tmp_path / "does-not-exist.db"),
    )
    assert n == 0


def test_record_injections_silent_on_unwritable_db(tmp_path: Path) -> None:
    # Directory that exists but cannot host a sqlite file: a regular file with
    # the name of a parent that already exists as a file. Use a path under a
    # non-directory to force sqlite3.connect() into failure.
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("blocking")
    db = bogus / "state.db"
    n = ledger.record_injections(
        "run-1", "node-1", None, None, None,
        attempt=1, sources=[{"kind": "gradient", "id": "g1"}],
        db=str(db),
    )
    assert n == 0


# ── injection_counts ───────────────────────────────────────────────────────────


def test_injection_counts_aggregates_uses_runs_last_ts(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    base = int(time.time())
    sources = [{"kind": "gradient", "id": "g1"}]
    # Two distinct runs, two attempts each.
    ledger.record_injections(
        "run-a", "n1", None, None, None, attempt=1, sources=sources,
        ts=base - 1000, db=str(db),
    )
    ledger.record_injections(
        "run-a", "n1", None, None, None, attempt=2, sources=sources,
        ts=base - 500, db=str(db),
    )
    ledger.record_injections(
        "run-b", "n1", None, None, None, attempt=1, sources=sources,
        ts=base - 200, db=str(db),
    )
    # A second source that should not show up.
    ledger.record_injections(
        "run-a", "n1", None, None, None, attempt=1,
        sources=[{"kind": "pattern", "id": "p1"}],
        ts=base - 100, db=str(db),
    )

    counts = ledger.injection_counts("gradient", ["g1", "missing"], db=str(db))
    assert set(counts) == {"g1"}
    assert counts["g1"]["uses"] == 3
    assert counts["g1"]["runs"] == 2
    assert counts["g1"]["held_out"] == 0
    assert counts["g1"]["last_ts"] == base - 200


def test_injection_counts_respects_since(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    base = int(time.time())
    sources = [{"kind": "gradient", "id": "g1"}]
    ledger.record_injections(
        "run-a", "n1", None, None, None, attempt=1, sources=sources,
        ts=base - 2000, db=str(db),
    )
    ledger.record_injections(
        "run-b", "n1", None, None, None, attempt=1, sources=sources,
        ts=base - 100, db=str(db),
    )

    counts = ledger.injection_counts(
        "gradient", ["g1"], since=base - 1000, db=str(db),
    )
    assert counts["g1"]["uses"] == 1
    assert counts["g1"]["runs"] == 1


def test_injection_counts_empty_source_ids_returns_empty(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    assert ledger.injection_counts("gradient", [], db=str(db)) == {}


# ── stage_health ───────────────────────────────────────────────────────────────


def _seed_pass_stats(db: Path, rows: list[tuple]) -> None:
    ledger.ensure_schema(str(db))
    con = sqlite3.connect(db)
    try:
        con.executemany(
            "INSERT INTO learning_pass_stats"
            "(pass_id, ts, stage, inputs, outputs, failures, lane, last_error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def test_stage_health_alarm_when_all_outputs_zero_with_inputs(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    now = int(time.time())
    _seed_pass_stats(db, [
        ("p1", now - 300, "pattern_induction", 5, 0, 1, "lane", "boom-1"),
        ("p2", now - 200, "pattern_induction", 4, 0, 1, "lane", "boom-2"),
        ("p3", now - 100, "pattern_induction", 3, 0, 0, "lane", ""),
    ])
    health = ledger.stage_health(window_passes=3, db=str(db))
    assert len(health) == 1
    h = health[0]
    assert h["stage"] == "pattern_induction"
    assert h["alarm"] is True
    # last_error comes from the newest row with failures > 0.
    assert h["last_error"] == "boom-2"
    # newest first
    assert [w["pass_id"] for w in h["window"]] == ["p3", "p2", "p1"]


def test_stage_health_no_alarm_when_one_output_present(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    now = int(time.time())
    _seed_pass_stats(db, [
        ("p1", now - 300, "pattern_induction", 5, 0, 1, "lane", "boom"),
        ("p2", now - 200, "pattern_induction", 4, 1, 0, "lane", ""),
        ("p3", now - 100, "pattern_induction", 3, 0, 0, "lane", ""),
    ])
    health = ledger.stage_health(window_passes=3, db=str(db))
    assert len(health) == 1
    assert health[0]["alarm"] is False


def test_stage_health_no_alarm_when_no_inputs(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    now = int(time.time())
    _seed_pass_stats(db, [
        ("p1", now - 200, "pattern_induction", 0, 0, 0, "lane", ""),
        ("p2", now - 100, "pattern_induction", 0, 0, 0, "lane", ""),
    ])
    health = ledger.stage_health(window_passes=3, db=str(db))
    assert len(health) == 1
    assert health[0]["alarm"] is False


def test_stage_health_window_truncates(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    now = int(time.time())
    rows = [
        (f"p{i}", now - (10 - i) * 10, "pattern_induction", 1, 0, 0, "lane", "")
        for i in range(10)
    ]
    _seed_pass_stats(db, rows)
    health = ledger.stage_health(window_passes=3, db=str(db))
    assert len(health[0]["window"]) == 3
    # newest first
    assert [w["pass_id"] for w in health[0]["window"]] == ["p9", "p8", "p7"]


# ── _write_learned_record integration ─────────────────────────────────────────


def test_write_learned_record_writes_ledger_rows_for_injected_block(
    tmp_path: Path, monkeypatch,
) -> None:
    from mini_ork.cli import execute_handlers

    db = _bare_db(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    # Ambient MINI_ORK_RUN_ID (from the enclosing mini-ork run) wins over
    # os.path.basename(run_dir); delenv so the assertion below sees the
    # basename path this test controls.
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)

    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    sources = [
        {"kind": "gradient", "id": "g1"},
        {"kind": "pattern", "id": "p1"},
    ]
    execute_handlers._write_learned_record(
        str(run_dir), "researcher-1", "researcher", "codex_lens",
        "framework_edit", attempt=1,
        block="INJECTED BLOCK CONTENT",
        sources=sources,
    )

    # JSON + MD present.
    assert (run_dir / "learned" / "researcher-1.json").exists()
    assert (run_dir / "learned" / "researcher-1.md").exists()

    # Ledger has one row per source.
    con = sqlite3.connect(db)
    try:
        rows = list(con.execute(
            "SELECT source_kind, source_id, run_id, node_id, attempt "
            "FROM lesson_injections ORDER BY source_kind"
        ))
    finally:
        con.close()
    assert len(rows) == 2
    assert rows[0][0] == "gradient"
    assert rows[1][0] == "pattern"
    assert rows[0][2] == "run-x"  # run_id from os.path.basename
    assert rows[0][3] == "researcher-1"


def test_write_learned_record_empty_block_writes_no_ledger_rows(
    tmp_path: Path, monkeypatch,
) -> None:
    from mini_ork.cli import execute_handlers

    db = _bare_db(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)
    # An empty block never calls the ledger, so the bare DB stays 0-byte with no
    # tables. ensure_schema builds them so the SELECT below has a real table
    # to COUNT — the assertion is "the empty path writes no rows", not "no
    # table exists".
    ledger.ensure_schema(str(db))

    run_dir = tmp_path / "run-y"
    run_dir.mkdir()
    sources = [{"kind": "gradient", "id": "g1"}]
    execute_handlers._write_learned_record(
        str(run_dir), "researcher-1", "researcher", "codex_lens",
        "framework_edit", attempt=1,
        block="",  # empty
        sources=sources,
    )
    con = sqlite3.connect(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM lesson_injections").fetchone()[0]
    finally:
        con.close()
    assert n == 0
    # JSON record still written.
    rec = json.loads(
        (run_dir / "learned" / "researcher-1.json").read_text()
    )
    assert rec["injected"] is False


def test_write_learned_record_swallows_unwritable_db(
    tmp_path: Path, monkeypatch,
) -> None:
    """A DB path that cannot be opened must not raise, and must still write
    the JSON + MD files. The ledger contract: 'never raise on the write path'."""
    from mini_ork.cli import execute_handlers

    # Force the DB resolution to a path whose parent is a regular file.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("MINI_ORK_DB", str(blocker / "state.db"))
    monkeypatch.delenv("MINI_ORK_RUN_ID", raising=False)

    run_dir = tmp_path / "run-z"
    run_dir.mkdir()
    sources = [{"kind": "gradient", "id": "g1"}]

    # Must not raise.
    execute_handlers._write_learned_record(
        str(run_dir), "researcher-1", "researcher", "codex_lens",
        "framework_edit", attempt=1,
        block="INJECTED BLOCK CONTENT",
        sources=sources,
    )

    # Files still written.
    assert (run_dir / "learned" / "researcher-1.json").exists()
    assert (run_dir / "learned" / "researcher-1.md").exists()


def test_write_learned_record_uses_mini_ork_run_id_when_set(
    tmp_path: Path, monkeypatch,
) -> None:
    from mini_ork.cli import execute_handlers

    db = _bare_db(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "explicit-run-id")

    run_dir = tmp_path / "run-rid"
    run_dir.mkdir()
    sources = [{"kind": "gradient", "id": "g1"}]
    execute_handlers._write_learned_record(
        str(run_dir), "researcher-1", "researcher", "codex_lens",
        "framework_edit", attempt=1,
        block="INJECTED",
        sources=sources,
    )

    con = sqlite3.connect(db)
    try:
        run_id = con.execute(
            "SELECT DISTINCT run_id FROM lesson_injections"
        ).fetchone()[0]
    finally:
        con.close()
    assert run_id == "explicit-run-id"


# ── ensure_schema on a bare DB ─────────────────────────────────────────────────


def test_ensure_schema_creates_tables_on_bare_db(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "bare.db")
    # Confirm starting state: file exists as 0-byte; sqlite can open it.
    ledger.ensure_schema(str(db))
    con = sqlite3.connect(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    finally:
        con.close()
    assert "lesson_injections" in names
    assert "learning_pass_stats" in names


def test_record_pass_stat_truncates_long_error(tmp_path: Path) -> None:
    db = _bare_db(tmp_path / "ledger.db")
    big = "x" * 5000
    ledger.record_pass_stat(
        "p1", "pattern_induction",
        inputs=1, outputs=0, failures=1, lane="codex_lens",
        last_error=big, db=str(db),
    )
    con = sqlite3.connect(db)
    try:
        stored = con.execute(
            "SELECT last_error FROM learning_pass_stats"
        ).fetchone()[0]
    finally:
        con.close()
    assert len(stored) == 500
    assert stored == big[-500:]


def test_record_pass_stat_silent_on_missing_db(tmp_path: Path) -> None:
    ledger.record_pass_stat(
        "p1", "pattern_induction",
        inputs=1, outputs=0, failures=0,
        db=str(tmp_path / "absent.db"),
    )  # must not raise
