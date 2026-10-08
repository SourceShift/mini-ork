"""Recover revival fixes (kickoff ``recover-revival-fixes``).

Three behaviours pinned here:

1. ``recover ``closes`` the dead attempt`` — a previous process that died
   mid-node leaves a dangling ``node_start``; ``cli_main`` writes the missing
   ``node_end`` *before* the recovered closure dispatches (else the
   stale-heartbeat watchdog aborts the revival on that dead heartbeat).
2. ``no zombie row`` — if the resumed execute exits (or raises) without a
   terminal status, the row is CAS-failed so the board never shows a dead run
   as ``executing``.
3. ``scoped watchdog`` — ``_watchdog_stale_heartbeat`` ignores heartbeats that
   predate this execute process (a previous attempt's), but still reports a
   real hang inside the current attempt.

Full schema via ``mini_ork.stores.migrate.init_db`` (the minimal ``SCHEMA_SQL``
used by the sibling recover tests has no ``run_events`` and no ``task_runs``
``verdict``/``notes`` columns). ``isolate_env`` clears the ambient ``MINI_ORK_*``
family so nothing leaks into the planner.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.recovery import planner as rp  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from test_recover_lease_wiring import _seed_success, _workflow  # noqa: E402

RUN = "run-revival-1"
RECIPE = "framework-edit"
TC = "framework_edit"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path):
    """Clear the ambient MINI_ORK_*/MO_* family — ``_watchdog_stale_heartbeat``
    reads ``MINI_ORK_DB`` *in preference to its argument*, so a leaked value
    silently ignores the tmp DB."""
    for key in (
        "MINI_ORK_RUN_DIR",
        "MINI_ORK_RECIPE",
        "MINI_ORK_WORKFLOW",
        "MINI_ORK_TASK_CLASS",
        "MINI_ORK_RUN_ID",
        "MINI_ORK_DB",
        "MINI_ORK_LEASE_TOKEN",
        "MINI_ORK_RECOVERY_REQUEST",
        "MO_EXEC_STARTED_MS",
        "MO_HEARTBEAT_TIMEOUT_S",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))


def _init_home(tmp_path: Path) -> Path:
    """A temp mini-ork home with a fully migrated ``state.db``."""
    home = tmp_path / ".mini-ork"
    home.mkdir()
    rc, _out, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, err
    return home


def _seed_heartbeat(db: str, node_id: str, hb_ms: int, *, node_type: str = "publisher") -> None:
    """One ``node_start`` for ``node_id`` with a heartbeat at ``hb_ms`` (ms) and
    no ``node_end`` — the shape a previous process left behind."""
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json, "
        "created_at, last_heartbeat_at) VALUES (?,?,?,?,?,?)",
        (f"evt-{node_id}", RUN, "node_start",
         json.dumps({"node_id": node_id, "node_type": node_type}), 1000, hb_ms))
    con.commit()
    con.close()


def _row(db: str) -> tuple[str, str | None]:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT status, notes FROM task_runs WHERE id = ?", (RUN,)).fetchone()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Scoped watchdog
# ─────────────────────────────────────────────────────────────────────────────


def test_watchdog_ignores_a_previous_attempts_heartbeat(tmp_path, monkeypatch):
    """A node whose last heartbeat predates this execute process belongs to a
    dead attempt — the watchdog must not abort the recovery on it."""
    home = _init_home(tmp_path)
    db = str(home / "state.db")
    now_ms = int(time.time() * 1000)
    _seed_heartbeat(db, "publisher", now_ms - 3_600_000)
    # This attempt started now; the heartbeat is an hour old → ignored.
    monkeypatch.setenv("MO_EXEC_STARTED_MS", str(now_ms))
    monkeypatch.setenv("MO_HEARTBEAT_TIMEOUT_S", "300")

    assert ex._watchdog_stale_heartbeat(str(REPO), db, RUN) == ""


def test_watchdog_still_reports_a_real_hang_in_this_attempt(tmp_path, monkeypatch):
    """A heartbeat that belongs to this process (>= its start) but is older than
    the timeout is a genuine hang and is still reported."""
    home = _init_home(tmp_path)
    db = str(home / "state.db")
    now_ms = int(time.time() * 1000)
    _seed_heartbeat(db, "publisher", now_ms - 60_000)
    monkeypatch.setenv("MO_EXEC_STARTED_MS", str(now_ms - 120_000))  # started 2 min ago
    monkeypatch.setenv("MO_HEARTBEAT_TIMEOUT_S", "5")                # stale after 5 s

    out = ex._watchdog_stale_heartbeat(str(REPO), db, RUN)

    assert out.startswith("publisher\t"), out


def test_watchdog_ignores_a_fresh_heartbeat(tmp_path, monkeypatch):
    """Guard against a filter that reports everything: a heartbeat inside the
    timeout window is not stale at all."""
    home = _init_home(tmp_path)
    db = str(home / "state.db")
    now_ms = int(time.time() * 1000)
    _seed_heartbeat(db, "publisher", now_ms)
    monkeypatch.setenv("MO_EXEC_STARTED_MS", str(now_ms - 120_000))
    monkeypatch.setenv("MO_HEARTBEAT_TIMEOUT_S", "300")

    assert ex._watchdog_stale_heartbeat(str(REPO), db, RUN) == ""


# ─────────────────────────────────────────────────────────────────────────────
# 1 + 2. cli_main: close the dead attempt, never leave a zombie
# ─────────────────────────────────────────────────────────────────────────────


def _setup_run(tmp_path, monkeypatch, *, status: str = "executing"):
    """A migrated home + a run whose closure is node D (A/B/C checkpointed
    success), a ``task_runs`` row at ``status``, and a dangling ``node_start``
    for ``publisher`` left by the dead attempt."""
    home = _init_home(tmp_path)
    db = str(home / "state.db")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": RECIPE}))
    wf = tmp_path / "wf.yaml"
    _workflow(wf)
    for node in ("A", "B", "C"):  # D failed → closure = {D}
        _seed_success(db, RUN, node, RECIPE, TC, str(run_dir))
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO task_runs(id, recipe, task_class, kickoff_path, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (RUN, RECIPE, TC, str(tmp_path / "k.md"), status, 1000, 1000))
    con.commit()
    con.close()
    _seed_heartbeat(db, "publisher", int(time.time() * 1000) - 3_600_000)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_TASK_CLASS", TC)
    monkeypatch.setenv("MINI_ORK_RECIPE", RECIPE)
    for key in ("MINI_ORK_LEASE_TOKEN", "MINI_ORK_RECOVERY_REQUEST", "MINI_ORK_WORKFLOW"):
        monkeypatch.delenv(key, raising=False)
    return db, str(wf)


def _count_node_end(db: str) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND event_type='node_end'",
            (RUN,)).fetchone()[0]
    finally:
        con.close()


def test_cli_closes_dangling_nodes_before_dispatch(tmp_path, monkeypatch, capsys):
    db, wf = _setup_run(tmp_path, monkeypatch)
    assert _count_node_end(db) == 0  # the attempt is dangling up front

    seen: dict = {}

    def stub(argv):
        # The node_end must already exist — the close happens BEFORE dispatch.
        seen["node_end"] = _count_node_end(db)
        seen["argv"] = list(argv)
        con = sqlite3.connect(db)
        con.execute("UPDATE task_runs SET status='published' WHERE id=?", (RUN,))
        con.commit()
        con.close()
        return 0

    rc = rp.cli_main([RUN, "--workflow", wf, "--db", db], execute_fn=stub)

    out = capsys.readouterr().out
    assert rc == 0
    assert seen["argv"] == ["--recovery"]
    assert seen["node_end"] >= 1, "node_end must be written before execute_fn runs"
    assert "closed 1 dangling node(s) from the previous attempt" in out
    assert _count_node_end(db) == 1  # exactly one synthetic close, no duplicates


def test_cli_marks_the_zombie_row_failed_on_nonzero_exit(tmp_path, monkeypatch):
    db, wf = _setup_run(tmp_path, monkeypatch, status="executing")

    rc = rp.cli_main([RUN, "--workflow", wf, "--db", db], execute_fn=lambda argv: 1)

    assert rc == 1
    status, notes = _row(db)
    assert status == "failed"
    assert notes and "recover: execute exited rc=1 without a terminal status" in notes


def test_cli_leaves_a_published_row_untouched(tmp_path, monkeypatch):
    db, wf = _setup_run(tmp_path, monkeypatch, status="executing")

    def stub(argv):
        con = sqlite3.connect(db)
        con.execute("UPDATE task_runs SET status='published' WHERE id=?", (RUN,))
        con.commit()
        con.close()
        return 0

    rc = rp.cli_main([RUN, "--workflow", wf, "--db", db], execute_fn=stub)

    assert rc == 0
    assert _row(db)[0] == "published"


def test_cli_leaves_an_executing_row_on_clean_exit(tmp_path, monkeypatch):
    """A clean ``rc=0`` recovery whose workflow wrote no terminal status is
    finished work, not a zombie: the row must be left exactly as it was. (The
    reaper is gated on ``exec_rc != 0``.)"""
    db, wf = _setup_run(tmp_path, monkeypatch, status="executing")

    rc = rp.cli_main([RUN, "--workflow", wf, "--db", db], execute_fn=lambda argv: 0)

    assert rc == 0
    assert _row(db)[0] == "executing"  # untouched — not 'failed'


def test_cli_leaves_a_cost_paused_row_for_resume(tmp_path, monkeypatch):
    """A cost-paused run waits on ``mini-ork resume``, not a reaper: even a
    failing exit must not fail its row (mirrors ``close_run_record``)."""
    db, wf = _setup_run(tmp_path, monkeypatch, status="executing")
    (tmp_path / "run" / ".cost-pause").write_text("", encoding="utf-8")

    rc = rp.cli_main([RUN, "--workflow", wf, "--db", db], execute_fn=lambda argv: 1)

    assert rc == 1
    assert _row(db)[0] == "executing"  # left for `mini-ork resume`


def test_cli_marks_failed_and_propagates_when_execute_raises(tmp_path, monkeypatch):
    db, wf = _setup_run(tmp_path, monkeypatch, status="executing")

    def stub(argv):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        rp.cli_main([RUN, "--workflow", wf, "--db", db], execute_fn=stub)

    # The finally-block reaper still ran before the exception propagated.
    assert _row(db)[0] == "failed"
