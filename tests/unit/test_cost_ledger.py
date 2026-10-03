"""Hermetic tests for ``mini_ork.cost_ledger.spent_last_24h``.

The ledger reader is the one definition of "rolling-24h USD spend" the budget
guards read. These tests build temp sqlite files with the minimal ``llm_calls``
and ``task_runs`` columns the reader touches, then verify the fallback ladder
(llm_calls → task_runs → 0.0) and the rolling-window predicate.

Timestamps are now-offset, never absolute: a literal-date test reds out once
that date ages past 24h.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork import cost_ledger  # noqa: E402


def _init_db(path: Path, *, with_llm_calls: bool = True, with_task_runs: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        if with_llm_calls:
            con.execute(
                "CREATE TABLE llm_calls("
                "id INTEGER PRIMARY KEY,"
                "run_id TEXT,"
                "cost_usd REAL,"
                "ts TEXT"
                ")"
            )
        if with_task_runs:
            con.execute(
                "CREATE TABLE task_runs("
                "id TEXT,"
                "cost_usd REAL,"
                "created_at INTEGER,"
                "updated_at INTEGER"
                ")"
            )
        con.commit()
    finally:
        con.close()
    return path


def _insert_llm_call(db: Path, *, run_id: str, cost: float, ts: str) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO llm_calls (id, run_id, cost_usd, ts) VALUES (?, ?, ?, ?)",
            (gen_id(), run_id, cost, ts),
        )
        con.commit()
    finally:
        con.close()


def _insert_task_run(db: Path, *, cost: float, created_at: int) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, cost_usd, created_at) VALUES (?, ?, ?)",
            (f"tr-{created_at}-{cost}", cost, created_at),
        )
        con.commit()
    finally:
        con.close()


def _iso(secs: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


_ID_COUNTER = [0]


def gen_id() -> int:
    _ID_COUNTER[0] += 1
    return _ID_COUNTER[0]


# ── ledger-vs-task_runs divergence (the root bug) ──────────────────────────────────


def test_ledger_includes_stage_rows_that_task_runs_misses(tmp_path: Path) -> None:
    """Measured on docs run 1791012731: task_runs said $0.39, llm_calls summed
    $1.04 across the profile answerer, worker, and rubric judge. The ledger
    must read the larger figure (1.04), not the smaller (0.39)."""
    db = _init_db(tmp_path / "ledger-stage")
    now = time.time()
    # Stage rows: only llm_calls has them; task_runs does not.
    _insert_llm_call(db, run_id="r1", cost=0.26, ts=_iso(now - 600))
    _insert_llm_call(db, run_id="r1", cost=0.39, ts=_iso(now - 1200))
    _insert_llm_call(db, run_id="r1", cost=0.39, ts=_iso(now - 1800))
    # What task_runs reports (the old under-counting meter):
    _insert_task_run(db, cost=0.39, created_at=int(now - 1200))

    assert cost_ledger.spent_last_24h(db) == pytest.approx(1.04)


# ── rolling-window predicate ──────────────────────────────────────────────────────


def test_rows_older_than_24h_are_excluded(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "ledger-window")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=0.50, ts=_iso(now - 600))
    _insert_llm_call(db, run_id="r1", cost=99.0, ts=_iso(now - 30 * 3600))

    assert cost_ledger.spent_last_24h(db) == pytest.approx(0.50)


# ── fallback ladder ───────────────────────────────────────────────────────────────


def test_falls_back_to_task_runs_when_llm_calls_missing(tmp_path: Path) -> None:
    """A DB behind the migrator degrades to the old estimate, not a blind zero."""
    db = _init_db(tmp_path / "ledger-fallback", with_llm_calls=False)
    now = int(time.time())
    _insert_task_run(db, cost=1.25, created_at=now - 3600)

    assert cost_ledger.spent_last_24h(db) == pytest.approx(1.25)


def test_no_db_path_returns_zero() -> None:
    assert cost_ledger.spent_last_24h(None) == 0.0
    assert cost_ledger.spent_last_24h("") == 0.0


def test_missing_file_returns_zero(tmp_path: Path) -> None:
    assert cost_ledger.spent_last_24h(tmp_path / "does-not-exist.db") == 0.0


def test_garbage_file_returns_zero(tmp_path: Path) -> None:
    """A non-SQLite file at the path yields 0.0, not a crash."""
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database at all")
    assert cost_ledger.spent_last_24h(garbage) == 0.0


# ── budget guards see the ledger sum (not task_runs) ──────────────────────────────


def test_cost_circuit_open_reads_ledger_sum(tmp_path: Path) -> None:
    """A db whose task_runs is under budget but whose llm_calls is over budget
    trips the guard — exactly the run-1791012731 bug class."""
    from mini_ork.dispatch.llm_dispatch import cost_circuit_open

    db = _init_db(tmp_path / "circuit-trip")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=12.5, ts=_iso(now - 600))
    # task_runs reports $0 — old meter would NOT trip.

    assert cost_circuit_open(str(db), "10") is True
    assert cost_circuit_open(str(db), "20") is False


def test_pre_iter_cost_check_reads_ledger_sum(tmp_path: Path) -> None:
    """pre_iter_cost_check mirrors the circuit. llm_calls over budget trips it."""
    from mini_ork.cli import self_improve

    db = _init_db(tmp_path / "pre-iter")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=7.0, ts=_iso(now - 600))

    # Env switch default is "1"; bypass the missing-file guard with a real file.
    assert self_improve.pre_iter_cost_check(str(db), "5") is True
    assert self_improve.pre_iter_cost_check(str(db), "10") is False


def test_today_cost_reads_ledger_sum(tmp_path: Path) -> None:
    """scheduler.today_cost_usd now delegates to cost_ledger.spent_last_24h."""
    from mini_ork import scheduler

    db = _init_db(tmp_path / "today")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=0.5, ts=_iso(now - 600))
    _insert_llm_call(db, run_id="r1", cost=0.3, ts=_iso(now - 1200))

    assert scheduler.today_cost_usd(str(db)) == pytest.approx(0.8)


def test_conductor_today_cost_reads_ledger_sum(tmp_path: Path) -> None:
    """orchestration.conductor._today_cost delegates to cost_ledger."""
    from mini_ork.orchestration import conductor

    db = _init_db(tmp_path / "conductor")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=2.0, ts=_iso(now - 600))

    assert conductor._today_cost(str(db)) == pytest.approx(2.0)


def test_cli_spent_last_24h_reads_ledger_sum(tmp_path: Path) -> None:
    """cli.main._spent_last_24h is the display mirror."""
    from mini_ork.cli import main as cli_main

    db = _init_db(tmp_path / "display")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=1.5, ts=_iso(now - 600))

    assert cli_main._spent_last_24h(str(db)) == pytest.approx(1.5)

def test_idle_wal_db_without_sidecar_files_is_still_read(tmp_path: Path) -> None:
    """A WAL db nobody has open has no -shm/-wal files. A ``mode=ro`` reader
    cannot create the -shm and fails, which read as $0 — the guard never trips."""
    import os

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE llm_calls(id INTEGER PRIMARY KEY, run_id TEXT, cost_usd REAL, ts TEXT)")
    con.execute("INSERT INTO llm_calls(run_id, cost_usd, ts) VALUES ('r1', 3.5, ?)", (_iso(time.time() - 60),))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    for suffix in ("-shm", "-wal"):
        side = tmp_path / f"state.db{suffix}"
        if side.exists():
            os.remove(side)

    assert cost_ledger.spent_last_24h(db) == pytest.approx(3.5)


# ── run_cost / reconcile_run_cost (run-cost-1791018816) ─────────────────────


def test_run_cost_sums_only_the_runs_rows(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "run-cost")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=0.50, ts=_iso(now - 600))
    _insert_llm_call(db, run_id="r1", cost=0.25, ts=_iso(now - 1200))
    _insert_llm_call(db, run_id="r2", cost=99.0, ts=_iso(now - 600))

    assert cost_ledger.run_cost(db, "r1") == pytest.approx(0.75)
    assert cost_ledger.run_cost(db, "r2") == pytest.approx(99.0)


def test_run_cost_returns_none_when_run_has_no_rows(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "run-cost-empty")
    _insert_llm_call(db, run_id="r1", cost=0.5, ts=_iso(time.time() - 600))

    assert cost_ledger.run_cost(db, "nope") is None


def test_run_cost_returns_none_when_llm_calls_table_missing(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "run-cost-no-table", with_llm_calls=False)

    assert cost_ledger.run_cost(db, "r1") is None


def test_run_cost_returns_none_for_missing_db(tmp_path: Path) -> None:
    assert cost_ledger.run_cost(None, "r1") is None
    assert cost_ledger.run_cost("", "r1") is None
    assert cost_ledger.run_cost(tmp_path / "does-not-exist.db", "r1") is None


def test_run_cost_returns_none_for_empty_run_id(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "run-cost-empty-id")
    _insert_llm_call(db, run_id="r1", cost=0.5, ts=_iso(time.time() - 600))

    assert cost_ledger.run_cost(db, "") is None


def test_run_cost_survives_wal_db_without_sidecar_files(tmp_path: Path) -> None:
    """Idle WAL with no -shm/-wal: same trap spent_last_24h avoids."""
    import os

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE llm_calls(id INTEGER PRIMARY KEY, run_id TEXT, cost_usd REAL, ts TEXT)")
    con.execute("INSERT INTO llm_calls(run_id, cost_usd, ts) VALUES ('r1', 2.5, ?)", (_iso(time.time() - 60),))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    for suffix in ("-shm", "-wal"):
        side = tmp_path / f"state.db{suffix}"
        if side.exists():
            os.remove(side)

    assert cost_ledger.run_cost(db, "r1") == pytest.approx(2.5)


def test_reconcile_run_cost_raises_stored_to_ledger_total(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "reconcile-raise")
    now = time.time()
    _insert_llm_call(db, run_id="run-raise", cost=0.40, ts=_iso(now - 600))
    _insert_llm_call(db, run_id="run-raise", cost=0.30, ts=_iso(now - 1200))
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, cost_usd, created_at) VALUES (?, ?, ?)",
            ("run-raise", 0.20, int(now - 600)),
        )
        con.commit()
    finally:
        con.close()

    assert cost_ledger.reconcile_run_cost(db, "run-raise") == pytest.approx(0.70)
    # Persisted to the task_runs row.
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT cost_usd FROM task_runs WHERE id = ?", ("run-raise",),
        ).fetchone()
    finally:
        con.close()
    assert row[0] == pytest.approx(0.70)
    # Second call is a no-op (stored already equals ledger).
    assert cost_ledger.reconcile_run_cost(db, "run-raise") == pytest.approx(0.70)


def test_reconcile_run_cost_never_lowers_stored(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "reconcile-no-lower")
    now = time.time()
    # Ledger says $0.10; node handler already wrote $5.00 — must stay $5.00.
    _insert_llm_call(db, run_id="r1", cost=0.10, ts=_iso(now - 600))
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, cost_usd, created_at) VALUES (?, ?, ?)",
            ("run-big", 5.00, int(now - 600)),
        )
        con.commit()
    finally:
        con.close()

    assert cost_ledger.reconcile_run_cost(db, "run-big") == pytest.approx(5.00)
    # Persisted value must be the larger stored number, not the ledger sum.
    assert cost_ledger.run_cost(db, "r1") == pytest.approx(0.10)


def test_reconcile_run_cost_unknown_run_returns_none(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "reconcile-unknown")
    _insert_llm_call(db, run_id="r1", cost=0.40, ts=_iso(time.time() - 600))
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, cost_usd, created_at) VALUES (?, ?, ?)",
            ("run-keep", 0.20, int(time.time()) - 600),
        )
        con.commit()
    finally:
        con.close()

    assert cost_ledger.reconcile_run_cost(db, "no-such-run") is None


def test_reconcile_run_cost_returns_none_for_missing_db(tmp_path: Path) -> None:
    assert cost_ledger.reconcile_run_cost(None, "r1") is None
    assert cost_ledger.reconcile_run_cost(tmp_path / "nope.db", "r1") is None


def test_reconcile_run_cost_bumps_updated_at(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "reconcile-bump")
    now = int(time.time())
    before = now - 600
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, cost_usd, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("r-old", 0.10, before, before),
        )
        con.execute(
            "INSERT INTO llm_calls (run_id, cost_usd, ts) VALUES (?, ?, ?)",
            ("r-old", 0.99, _iso(now - 60)),
        )
        con.commit()
    finally:
        con.close()

    after = cost_ledger.reconcile_run_cost(db, "r-old")
    assert after == pytest.approx(0.99)

    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT cost_usd, updated_at FROM task_runs WHERE id = ?", ("r-old",),
        ).fetchone()
    finally:
        con.close()
    assert row[0] == pytest.approx(0.99)
    assert row[1] > before


def test_lifecycle_reconcile_publishes_cost_usd_to_sink(tmp_path, monkeypatch):
    """`_run_lifecycle` calls reconcile at the end; mini_ork_result carries cost_usd."""
    from mini_ork.cli import main as cli_main

    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    # Migrate the db so _open_db / StateDB find a real schema.
    from mini_ork.stores.migrate import init_db

    rc, out, err = init_db(db=str(db), root=str(REPO))
    assert rc == 0, f"init_db rc={rc}\nstdout={out}\nstderr={err}"

    # Production llm_calls.run_id is INTEGER; pick a numeric run_id so the
    # reconcile can match the rows.
    run_id = 42
    import sqlite3 as _sq
    con = _sq.connect(str(db))
    try:
        con.execute(
            "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, "
            "cost_usd, created_at, updated_at) VALUES (?, 'x', 'r', 'k', 'published', "
            "0.10, strftime('%s','now'), strftime('%s','now'))",
            (str(run_id),),
        )
        con.execute(
            "INSERT INTO llm_calls (provider, model_id, tier, feature_name, "
            "status, run_id, cost_usd, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("anthropic", "test", "default", "test", "success",
             run_id, 0.74, time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 60))),
        )
        con.commit()
    finally:
        con.close()

    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "0")

    # Capture --json output via stdout.
    import io as _io

    captured: dict = {}

    def fake_impl(argv, root, sink):
        del argv, root
        sink["run_id"] = run_id
        sink["verdict"] = "pass"
        return 0

    monkeypatch.setattr(cli_main, "_run_lifecycle_impl", fake_impl)

    real_stdout = sys.stdout
    buf = _io.StringIO()
    sys.stdout = buf
    try:
        rc = cli_main._run_lifecycle(["--json"], str(REPO))
    finally:
        sys.stdout = real_stdout
    assert rc == 0
    line = buf.getvalue().strip().splitlines()[-1]
    assert line.startswith("mini_ork_result=")
    captured["sink"] = json.loads(line[len("mini_ork_result="):])
    assert captured["sink"]["cost_usd"] == pytest.approx(0.74)


def test_lifecycle_reconcile_dry_run_does_not_reconcile(monkeypatch):
    """MINI_ORK_DRY_RUN=1 skips the reconcile call — sink stays without cost_usd."""
    from mini_ork.cli import main as cli_main

    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    import io as _io

    def fake_impl(argv, root, sink):
        del argv, root
        sink["run_id"] = "run-dry"
        sink["verdict"] = "pass"
        return 0

    monkeypatch.setattr(cli_main, "_run_lifecycle_impl", fake_impl)

    real_stdout = sys.stdout
    buf = _io.StringIO()
    sys.stdout = buf
    try:
        rc = cli_main._run_lifecycle(["--json"], str(REPO))
    finally:
        sys.stdout = real_stdout
    assert rc == 0
    sink = json.loads(buf.getvalue().strip().splitlines()[-1][len("mini_ork_result="):])
    assert "cost_usd" not in sink


def test_lifecycle_reconcile_error_does_not_change_rc(tmp_path, monkeypatch):
    """If reconcile raises, the lifecycle still returns the original rc."""
    from mini_ork.cli import main as cli_main

    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "0")

    def boom(sink):
        del sink
        raise RuntimeError("simulated db corruption")

    monkeypatch.setattr(cli_main, "_reconcile_run_cost", boom)

    def fake_impl(argv, root, sink):
        del argv, root
        sink["run_id"] = "run-boom"
        return 7

    monkeypatch.setattr(cli_main, "_run_lifecycle_impl", fake_impl)
    import io as _io

    real_stdout = sys.stdout
    sys.stdout = _io.StringIO()
    try:
        rc = cli_main._run_lifecycle(["--json"], str(REPO))
    finally:
        sys.stdout = real_stdout
    assert rc == 7
