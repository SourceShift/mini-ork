"""RB-33/RB-49: ``duration_ms`` is the run's ACTIVE time on every close path.

Hermetic tmp sqlite only — no live DB, no network, no provider credentials.
Each test seeds the row shapes the kickoff names and asserts the close path's
active-time arithmetic.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from mini_ork.cli.execute import set_status as execute_set_status
from mini_ork.cli.plan import _mark_failed as plan_mark_failed
from mini_ork.cli.publisher import _mark_published
from mini_ork.orchestration import run_reaper
from mini_ork.web.control import _restore_status, kill_run
from mini_ork.web.db import StateDB

_TASK_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
  id                TEXT PRIMARY KEY,
  task_class        TEXT NOT NULL DEFAULT 'framework_edit',
  recipe            TEXT,
  workflow_version  TEXT NOT NULL DEFAULT 'latest',
  kickoff_path      TEXT NOT NULL DEFAULT '/tmp/kickoff.md',
  status            TEXT NOT NULL DEFAULT 'classified'
                    CHECK (status IN ('classified','planned','executing','verifying',
                                      'reviewing','published','rolled_back','failed',
                                      'not_needed')),
  verdict           TEXT,
  cost_usd          REAL NOT NULL DEFAULT 0,
  duration_ms       INTEGER NOT NULL DEFAULT 0,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,
  ended_at          INTEGER,
  notes             TEXT
);
"""

_RUN_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS run_events (
  event_id       TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL,
  parent_run_id  TEXT,
  event_type     TEXT NOT NULL,
  payload_json   TEXT NOT NULL DEFAULT '{}',
  created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""

IDLE = 3 * 86400


def _make_db(tmp_path) -> str:
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    try:
        con.executescript(_TASK_RUNS_DDL)
        con.executescript(_RUN_EVENTS_DDL)
        con.commit()
    finally:
        con.close()
    return str(db)


def _seed(db, run_id, *, status="executing", verdict=None, duration_ms=0,
          created_at, updated_at=None, ended_at=None, notes=None):
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, status, verdict, duration_ms, created_at,"
            " updated_at, ended_at, notes) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, status, verdict, duration_ms, created_at,
             created_at if updated_at is None else updated_at, ended_at, notes))
        con.commit()
    finally:
        con.close()


def _fetch(db, run_id) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        con.close()
    assert row is not None
    return dict(row)


def _utc_iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _attempt_note(n: int, ts: int) -> str:
    return f"attempt {n} started {_utc_iso(ts)}"


def _assert_ms_close(actual, expected_ms, *, tol_ms=5000):
    assert actual is not None
    lo, hi = expected_ms - tol_ms, expected_ms + tol_ms
    assert lo <= actual <= hi, f"{actual} ms outside [{lo}, {hi}]"


def _seed_two_attempt_run(db, run_id, *, now, attempt2_s=50, heartbeat_after=30):
    """Attempt 1 ran 100 s, 3 idle days, attempt 2 is ~50 s old at close time.

    ``updated_at`` is deliberately newer than the attempt-2 marker so a close
    keyed off ``updated_at`` instead of the marker reads short.
    """
    t1 = now - IDLE - 100 - attempt2_s
    t2 = now - attempt2_s
    _seed(db, run_id, status="executing", duration_ms=100000, created_at=t1,
          updated_at=t2 + heartbeat_after,
          notes="; ".join([_attempt_note(1, t1), _attempt_note(2, t2)]))
    return {"t1": t1, "t2": t2, "updated_at": t2 + heartbeat_after}


# ── RB-33: kill / plan-stage failure no longer record 0 ─────────────────────


def test_kill_120s_after_creation_records_active_time(tmp_path):
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000001-1111"
    _seed(db, run_id, status="executing", created_at=now - 120,
          updated_at=now - 120, notes=_attempt_note(1, now - 120))

    result = kill_run(tmp_path / "home", StateDB(Path(db)), run_id)

    assert result.get("ok") is True, result
    row = _fetch(db, run_id)
    assert row["status"] == "failed"
    assert row["ended_at"] is not None
    _assert_ms_close(row["duration_ms"], 120000)


def test_kill_of_revived_run_adds_only_the_final_attempt(tmp_path):
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000002-2222"
    t1 = now - IDLE - 100 - 20
    t2 = now - 20
    _seed(db, run_id, status="executing", duration_ms=100000, created_at=t1,
          updated_at=t2,
          notes="; ".join([_attempt_note(1, t1), _attempt_note(2, t2)]))

    result = kill_run(tmp_path / "home", StateDB(Path(db)), run_id)

    assert result.get("ok") is True, result
    row = _fetch(db, run_id)
    assert row["status"] == "failed"
    _assert_ms_close(row["duration_ms"], 120000)


def test_plan_mark_failed_30s_after_creation_records_active_time(tmp_path):
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000020-2020"
    _seed(db, run_id, status="planned", created_at=now - 30, updated_at=now - 30)

    plan_mark_failed(db, run_id, "CRASH")

    row = _fetch(db, run_id)
    assert row["status"] == "failed"
    assert row["ended_at"] is not None
    _assert_ms_close(row["duration_ms"], 30000)


# ── RB-33: every terminal close sums attempts, never the idle gap ───────────


def test_set_status_failed_sums_attempts_without_idle(tmp_path):
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000010-1010"
    _seed_two_attempt_run(db, run_id, now=now)

    execute_set_status(db, run_id, "failed")

    row = _fetch(db, run_id)
    assert row["status"] == "failed"
    assert row["ended_at"] is not None
    _assert_ms_close(row["duration_ms"], 150000)


def test_close_reads_the_marker_when_a_note_follows_it(tmp_path):
    """A trailing note must not swallow the marker's ``;`` separator.

    Notes are ``"; "``-joined, so a mid-attempt breadcrumb
    (``impl_moved_base``, ``VERIFIER_NOT_EXECUTED``, …) sits after the last
    ``attempt n started`` segment. A ``\\S+`` capture ate the ``;``, the stamp
    failed to parse, the marker was silently skipped, and the close fell back
    to ``updated_at``/``created_at`` — measuring 120000 instead of 150000.
    """
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000016-1616"
    t1 = now - IDLE - 100 - 50
    t2 = now - 50
    _seed(db, run_id, status="executing", duration_ms=100000, created_at=t1,
          updated_at=t2 + 30,
          notes="; ".join([_attempt_note(1, t1), _attempt_note(2, t2),
                           "impl_moved_base: origin/main advanced"]))

    execute_set_status(db, run_id, "failed")

    _assert_ms_close(_fetch(db, run_id)["duration_ms"], 150000)


def test_review_park_keeps_the_unparked_attempt_time(tmp_path):
    """executing → reviewing → approve (recover resume) → publish.

    ``reviewing`` parks without closing; the resumed ``executing`` write starts
    a new marker on a row that is still live, so the time between the first
    marker and the park must be folded into ``duration_ms`` before it does.
    Reproduced before the fix: 0 ms for a genuine 1100 s of work.
    """
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000017-1717"
    t_start = now - 1700
    parked = now - 600
    # ``set_status('reviewing')`` parked the row here: still live (no
    # ``ended_at``), ``updated_at`` = the park write, one marker.
    _seed(db, run_id, status="reviewing", created_at=t_start,
          updated_at=parked, notes=_attempt_note(1, t_start))
    # The approve path resumes the run, which re-enters ``executing``.
    execute_set_status(db, run_id, "executing")
    _mark_published(db, run_id)

    row = _fetch(db, run_id)
    assert row["status"] == "published"
    _assert_ms_close(row["duration_ms"], 1100000, tol_ms=10000)


def test_close_never_exceeds_the_rows_wall_age(tmp_path):
    """A legacy inflated duration_ms cannot outlive the row itself."""
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000018-1818"
    _seed(db, run_id, status="executing", duration_ms=9_000_000,
          created_at=now - 1000, updated_at=now,
          notes=_attempt_note(1, now - 100))

    execute_set_status(db, run_id, "failed")

    _assert_ms_close(_fetch(db, run_id)["duration_ms"], 1_000_000, tol_ms=1000)


def test_publisher_mark_published_sums_attempts_without_idle(tmp_path):
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000011-1111"
    _seed_two_attempt_run(db, run_id, now=now)

    _mark_published(db, run_id)

    row = _fetch(db, run_id)
    assert row["status"] == "published"
    assert row["verdict"] == "APPROVE"
    assert row["ended_at"] is not None
    _assert_ms_close(row["duration_ms"], 150000)


def test_reaper_finalize_passed_sums_attempts_without_idle(tmp_path):
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000012-1212"
    stamps = _seed_two_attempt_run(db, run_id, now=now)

    closed = run_reaper.finalize_passed(
        db, run_id, "passed with no publisher node", now=stamps["t2"] + 50)

    assert closed is True
    row = _fetch(db, run_id)
    assert row["status"] == "published"
    assert row["ended_at"] == stamps["t2"] + 50
    _assert_ms_close(row["duration_ms"], 150000)


def test_reaper_mark_failed_sums_attempts_without_idle(tmp_path):
    now = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000013-1313"
    stamps = _seed_two_attempt_run(db, run_id, now=now)

    con = sqlite3.connect(db)
    try:
        closed = run_reaper._mark_failed(
            con, run_id, "executing", stamps["updated_at"], "CRASH",
            "reaped: owner provably died",
            ended_at=stamps["t2"] + 50, now=stamps["t2"] + 50)
    finally:
        con.close()

    assert closed is True
    row = _fetch(db, run_id)
    assert row["status"] == "failed"
    assert row["ended_at"] == stamps["t2"] + 50
    _assert_ms_close(row["duration_ms"], 150000)


def test_attempt_start_falls_back_to_updated_at_without_a_marker(tmp_path):
    fixed = int(time.time())
    db = _make_db(tmp_path)
    run_id = "run-1799000014-1414"
    _seed(db, run_id, status="executing", created_at=fixed - 1000,
          updated_at=fixed - 40, notes="impl_moved_base: seeded row")

    closed = run_reaper.finalize_passed(db, run_id, "passed", now=fixed)

    assert closed is True
    _assert_ms_close(_fetch(db, run_id)["duration_ms"], 40000, tol_ms=1000)


def test_live_set_status_keeps_accumulated_duration(tmp_path):
    now = int(time.time())
    t1 = now - 86400
    db = _make_db(tmp_path)
    run_id = "run-1799000015-1515"
    _seed(db, run_id, status="failed", verdict="CRASH", duration_ms=100000,
          created_at=t1, updated_at=t1 + 100, ended_at=t1 + 100,
          notes=_attempt_note(1, t1))

    execute_set_status(db, run_id, "executing")

    row = _fetch(db, run_id)
    assert row["status"] == "executing"
    assert row["duration_ms"] == 100000
    assert row["ended_at"] is None
    assert row["verdict"] is None
    assert "attempt 2 started" in (row["notes"] or "")


def test_live_restore_keeps_accumulated_duration(tmp_path):
    now = int(time.time())
    t1 = now - 86400
    db = _make_db(tmp_path)
    run_id = "run-1799000030-3030"
    _seed(db, run_id, status="not_needed", verdict="CRASH", duration_ms=100000,
          created_at=t1, updated_at=t1 + 100, ended_at=t1 + 100,
          notes=_attempt_note(1, t1))

    _restore_status(db, run_id, "executing")

    row = _fetch(db, run_id)
    assert row["status"] == "executing"
    assert row["duration_ms"] == 100000
    assert row["ended_at"] is None
    assert row["verdict"] is None
    assert "attempt 2 started" in (row["notes"] or "")


# ── RB-49: kill frees the recorded provider node ────────────────────────────


def _seed_killable(tmp_path, run_id):
    db = _make_db(tmp_path)
    now = int(time.time())
    _seed(db, run_id, status="executing", created_at=now - 60, updated_at=now - 60)
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / ".node-provider.json").write_text(json.dumps({
        "provider": "daytona",
        "handle": "ws-spec-1",
        "run_id": run_id,
        "created_at": now,
    }), encoding="utf-8")
    return db, home, run_dir


def test_kill_run_releases_the_recorded_node_and_reports_it(tmp_path, monkeypatch):
    from mini_ork.remote.providers import base as provider_base
    from mini_ork.remote.providers import run_nodes as provider_run_nodes

    run_id = "run-1799000003-3333"
    db, home, run_dir = _seed_killable(tmp_path, run_id)
    calls = {"run_node": [], "all_nodes": []}

    def fake_release_run_node(run_dir_arg, env):
        calls["run_node"].append((run_dir_arg, env))
        return True

    def fake_release_all_nodes(run_dir_arg, env):
        calls["all_nodes"].append((run_dir_arg, env))
        return 1

    monkeypatch.setattr(provider_base, "release_run_node", fake_release_run_node)
    monkeypatch.setattr(provider_run_nodes, "release_all_nodes", fake_release_all_nodes)

    result = kill_run(home, StateDB(Path(db)), run_id)

    assert calls["run_node"] and calls["all_nodes"]
    assert Path(calls["run_node"][0][0]) == run_dir
    assert Path(calls["all_nodes"][0][0]) == run_dir
    assert result.get("nodes_released")


def test_kill_run_release_failure_never_raises_and_still_fails_the_run(
        tmp_path, monkeypatch):
    from mini_ork.remote.providers import base as provider_base
    from mini_ork.remote.providers import run_nodes as provider_run_nodes

    run_id = "run-1799000004-4444"
    db, home, _run_dir = _seed_killable(tmp_path, run_id)

    def boom(*_args, **_kwargs):
        raise RuntimeError("daytona unreachable")

    monkeypatch.setattr(provider_base, "release_run_node", boom)
    monkeypatch.setattr(provider_run_nodes, "release_all_nodes", boom)

    result = kill_run(home, StateDB(Path(db)), run_id)

    assert result.get("ok") is True, result
    assert "nodes_released" in result
    assert _fetch(db, run_id)["status"] == "failed"
