"""RB-39 — telemetry readers must read tables (and columns) that are written.

Three writers lost in the bash→Python port are revived here:

  1. ``stream.py`` reads the run_events bridge (run_detail keeps its legacy mo_events bridge for pre-port history) instead of the
     never-written ``mo_events`` table.
  2. ``llm_dispatch._halt_on_fuse`` stamps ``task_runs.fuse_blown_lane`` and
     increments ``fuse_consecutive_failures`` for the run that hit the fuse.
  3. the run lifecycle's failed close calls ``memory.store.write_failure``.

Every test is hermetic: a throwaway sqlite file under ``tmp_path``, every
run-scoped env var fenced on both layers (os.environ via monkeypatch and the
run-context contextvar via ``run_context_scope``), no network. There is no
pytest-asyncio in this venv, so the SSE generator is driven with
``asyncio.run`` and a request stub whose ``is_disconnected`` returns True on
the second poll.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.context import run_context_scope

# ── schema fixtures ─────────────────────────────────────────────────────────

TASK_RUNS_DDL = """
CREATE TABLE task_runs (
  id TEXT PRIMARY KEY,
  task_class TEXT NOT NULL,
  recipe TEXT,
  workflow_version TEXT NOT NULL DEFAULT 'latest',
  kickoff_path TEXT NOT NULL DEFAULT '',
  plan_path TEXT, plan_hash TEXT, artifact_path TEXT, artifact_hash TEXT,
  status TEXT NOT NULL DEFAULT 'classified',
  verdict TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  trace_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  ended_at INTEGER,
  parent_epic_id TEXT,
  notes TEXT,
  dispatch_config_json TEXT,
  agents_yaml_sha TEXT,
  fuse_blown_lane TEXT,
  fuse_consecutive_failures INTEGER NOT NULL DEFAULT 0
)
"""

RUN_EVENTS_DDL = """
CREATE TABLE run_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  parent_run_id TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
)
"""

MO_EVENTS_DDL = """
CREATE TABLE mo_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER,
  trace_id TEXT,
  event_type TEXT,
  actor TEXT,
  status TEXT,
  duration_ms INTEGER,
  cost_usd REAL,
  artifact_path TEXT,
  payload_json TEXT
)
"""

LLM_CALLS_DDL = """
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL,
  tier TEXT NOT NULL DEFAULT 'default',
  feature_name TEXT NOT NULL DEFAULT '',
  actor TEXT,
  epic_id TEXT, dispatch_id INTEGER, run_id INTEGER, iter INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
  thinking_tokens INTEGER,
  cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'success',
  finish_reason TEXT,
  error_message TEXT,
  error_category TEXT,
  retryable INTEGER,
  traceparent TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""

FAILURE_MEMORY_DDL = """
CREATE TABLE failure_memory (
  failure_id TEXT PRIMARY KEY,
  run_id INTEGER NOT NULL,
  workflow_stage TEXT NOT NULL,
  failure_category TEXT NOT NULL,
  error_message TEXT NOT NULL DEFAULT '',
  stack_trace TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""

RUNS_DDL = "CREATE TABLE runs (id INTEGER PRIMARY KEY, run_dir TEXT NOT NULL UNIQUE)"

FENCED_ENV = (
    "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_DB", "MINI_ORK_HOME",
    "MINI_ORK_ROOT", "MO_STORE_DB", "MINI_ORK_DEFAULT_MODEL",
    "MO_PLACEMENT", "MO_NODE_ENV", "MO_TARGET_CWD",
    "MO_FUSE_ENABLED", "MO_FUSE_TERMINAL", "MO_FUSE_TRANSIENT",
    "MO_FUSE_WINDOW_HOURS", "MO_DAILY_BUDGET_USD", "MINI_ORK_DRY_RUN",
)


def _fence_env(monkeypatch: pytest.MonkeyPatch, **bindings: str | None) -> None:
    """Clear every run-scoped var, then apply ``bindings`` on both layers."""
    for var in FENCED_ENV:
        monkeypatch.delenv(var, raising=False)
    scope: dict[str, str | None] = {var: None for var in FENCED_ENV}
    for key, value in bindings.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
            scope[key] = value


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=10.0)
    con.execute("PRAGMA busy_timeout=10000")
    con.row_factory = sqlite3.Row
    return con


def _create_db(db_path: Path, *ddls: str) -> None:
    con = _connect(db_path)
    try:
        for ddl in ddls:
            con.execute(ddl)
        con.commit()
    finally:
        con.close()


def _insert_task_run(db_path: Path, **cols: object) -> None:
    now = int(time.time())
    row: dict[str, object] = {
        "task_class": "spec_task_class", "recipe": "spec-recipe",
        "status": "executing", "trace_id": None, "verdict": None,
        "notes": None, "created_at": now, "updated_at": now, "ended_at": None,
        "cost_usd": 0.0, "duration_ms": 0,
        "fuse_blown_lane": None, "fuse_consecutive_failures": 0,
    }
    row.update(cols)
    con = _connect(db_path)
    try:
        con.execute(
            f"INSERT INTO task_runs ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        con.commit()
    finally:
        con.close()


def _insert_run_event(db_path: Path, event_id: str, run_id: str,
                      event_type: str, payload: dict, created_at: int) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (event_id, run_id, event_type, json.dumps(payload), created_at),
        )
        con.commit()
    finally:
        con.close()


# ── mechanism 1: the stream reads run_events, never mo_events ──────────────

STREAM_RUN = "unit-stream-run"
STREAM_TRACE = "unit-trace-stream"


class _FakeRequest:
    """Request stub whose client disconnects after ``polls`` loop checks."""

    def __init__(self, polls: int = 1) -> None:
        self._polls = polls
        self._checked = 0

    async def is_disconnected(self) -> bool:
        self._checked += 1
        return self._checked > self._polls


def _parse_sse(frame: str) -> tuple[str, dict]:
    name, data = "", {}
    for line in frame.splitlines():
        if line.startswith("event: "):
            name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
    return name, data


def _collect_stream_frames(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    run_id: str,
    after_hello,
) -> list[str]:
    import mini_ork.web.routes.stream as stream_mod
    from mini_ork.web.db import StateDB

    monkeypatch.setattr(stream_mod, "POLL_INTERVAL_S", 0.02, raising=False)
    sdb = StateDB(db_path)
    request = _FakeRequest(polls=1)

    async def _drive() -> list[str]:
        gen = stream_mod._event_loop(
            db=sdb, request=request, task_run_id=run_id, nodes=None, home=None
        )
        frames = [await gen.__anext__()]  # hello: cursors snapshot here
        after_hello()
        try:
            while True:
                frames.append(await gen.__anext__())
        except StopAsyncIteration:
            pass
        return frames

    try:
        return asyncio.run(_drive())
    finally:
        sdb.close()


def test_stream_returns_run_events_rows_with_mo_events_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    _create_db(db_path, TASK_RUNS_DDL, RUN_EVENTS_DDL, MO_EVENTS_DDL)
    _insert_task_run(db_path, id=STREAM_RUN, trace_id=STREAM_TRACE)
    now = int(time.time())

    def _after_hello() -> None:
        _insert_run_event(
            db_path, "evt-unit-1", STREAM_RUN, "node_start",
            {"node_id": "researcher-one", "node_type": "researcher"}, now,
        )

    frames = _collect_stream_frames(monkeypatch, db_path, STREAM_RUN, _after_hello)
    parsed = [_parse_sse(f) for f in frames]

    assert parsed and parsed[0][0] == "hello", parsed[:1]
    events = [data for name, data in parsed if name == "event"]
    matched = [
        d for d in events
        if d.get("event_type") == "node_start" and str(d.get("id")) == "evt-unit-1"
    ]
    assert matched, f"run_events row not streamed; frames: {parsed}"
    assert matched[0].get("ts") is not None, matched[0]
    assert "payload_json" in matched[0], matched[0]

    con = _connect(db_path)
    try:
        assert con.execute("SELECT COUNT(*) FROM mo_events").fetchone()[0] == 0
    finally:
        con.close()


def test_stream_does_not_replay_mo_events_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    _create_db(db_path, TASK_RUNS_DDL, RUN_EVENTS_DDL, MO_EVENTS_DDL)
    _insert_task_run(db_path, id=STREAM_RUN, trace_id=STREAM_TRACE)
    now = int(time.time())

    def _after_hello() -> None:
        _insert_run_event(
            db_path, "evt-unit-2", STREAM_RUN, "node_start",
            {"node_id": "researcher-one", "node_type": "researcher"}, now,
        )
        con = _connect(db_path)
        try:
            con.execute(
                "INSERT INTO mo_events (ts, trace_id, event_type, payload_json) "
                "VALUES (?,?,?,?)",
                (now, STREAM_TRACE, "mo_only_decoy", "{}"),
            )
            con.commit()
        finally:
            con.close()

    frames = _collect_stream_frames(monkeypatch, db_path, STREAM_RUN, _after_hello)
    events = [data for name, data in (_parse_sse(f) for f in frames) if name == "event"]

    assert any(d.get("event_type") == "node_start" for d in events)
    assert not [d for d in events if d.get("event_type") == "mo_only_decoy"], (
        f"stream still replays mo_events rows: {events}"
    )


DETAIL_RUN = "unit-detail-run"
DETAIL_TRACE = "unit-trace-detail"


# ── mechanism 2: a fuse halt stamps the run's task_runs row ────────────────

FUSE_RUN = "unit-fuse-run"
FUSE_LANE = "unit-dead-lane"


def _seed_fuse_db(db_path: Path) -> None:
    _create_db(db_path, TASK_RUNS_DDL, LLM_CALLS_DDL)
    _insert_task_run(db_path, id=FUSE_RUN, trace_id="unit-trace-fuse")
    con = _connect(db_path)
    try:
        # One terminal-category strike ('quota', 1 strike by default) inside the
        # fuse window opens the lane fuse for FUSE_LANE.
        con.execute(
            "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, "
            "  status, error_message, error_category, retryable, cost_usd, "
            "  metadata_json, ts) "
            "VALUES ('spec', ?, 'default', 'mini-ork:researcher', NULL, 'failed', "
            "  'spec quota wall', 'quota', 0, 0.0, '{}', "
            "  strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (FUSE_LANE,),
        )
        con.commit()
    finally:
        con.close()


def _fuse_row(db_path: Path) -> tuple[object, int]:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT fuse_blown_lane, fuse_consecutive_failures FROM task_runs "
            "WHERE id = ?", (FUSE_RUN,)
        ).fetchone()
    finally:
        con.close()
    assert row is not None, "fixture task_runs row vanished"
    return row[0], int(row[1])


def _dispatch_on_fused_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str | None
) -> int:
    from mini_ork.dispatch.llm_dispatch import llm_dispatch

    bindings = {
        "MINI_ORK_DB": str(tmp_path / "state.db"),
        "MINI_ORK_HOME": str(tmp_path),
        "MO_FUSE_ENABLED": "1",
    }
    if run_id is not None:
        bindings["MINI_ORK_RUN_ID"] = run_id
    _fence_env(monkeypatch, **bindings)
    with run_context_scope(dict(bindings)):
        return llm_dispatch(
            ["--model", FUSE_LANE, "--prompt-text", "unit probe",
             "--out", str(tmp_path / "out.txt"), "--timeout", "5"]
        )


def test_fuse_halt_sets_blown_lane_and_increments_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    _seed_fuse_db(db_path)

    assert _dispatch_on_fused_lane(tmp_path, monkeypatch, FUSE_RUN) == 43
    lane1, count1 = _fuse_row(db_path)
    assert lane1 == FUSE_LANE, f"fuse_blown_lane={lane1!r}"
    assert count1 == 1, f"fuse_consecutive_failures={count1}"

    assert _dispatch_on_fused_lane(tmp_path, monkeypatch, FUSE_RUN) == 43
    lane2, count2 = _fuse_row(db_path)
    assert lane2 == FUSE_LANE
    assert count2 == 2, f"second halt must increment (expected 2), got {count2}"


def test_fuse_halt_without_run_id_leaves_task_runs_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    _seed_fuse_db(db_path)

    assert _dispatch_on_fused_lane(tmp_path, monkeypatch, None) == 43
    lane, count = _fuse_row(db_path)
    assert (lane, count) == (None, 0)


def test_fuse_halt_for_unknown_run_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    _seed_fuse_db(db_path)

    assert _dispatch_on_fused_lane(tmp_path, monkeypatch, "unit-no-such-run") == 43
    lane, count = _fuse_row(db_path)
    assert (lane, count) == (None, 0)


# ── mechanism 3: a failed close writes one failure_memory row ──────────────

CLOSE_RUN = "unit-close-run"
CLOSE_RUNS_ID = 41


def _seed_close_db(db_path: Path, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    _create_db(db_path, TASK_RUNS_DDL, RUN_EVENTS_DDL, MO_EVENTS_DDL,
               FAILURE_MEMORY_DDL, RUNS_DDL)
    now = int(time.time())
    _insert_task_run(db_path, id=CLOSE_RUN, task_class="spec_task_class",
                     recipe="spec-recipe", trace_id="unit-trace-close",
                     created_at=now - 3000, updated_at=now - 3000)
    _insert_run_event(db_path, "evt-close-1", CLOSE_RUN, "node_end",
                      {"node_id": "planner", "node_type": "planner",
                       "verdict": "pass", "status": "success"}, now - 2000)
    _insert_run_event(db_path, "evt-close-2", CLOSE_RUN, "node_end",
                      {"node_id": "researcher-one", "node_type": "researcher",
                       "verdict": "fail", "status": "failed"}, now - 1500)
    _insert_run_event(db_path, "evt-close-3", CLOSE_RUN, "node_end",
                      {"node_id": "implementer-two", "node_type": "implementer",
                       "verdict": "fail", "status": "failed"}, now - 1000)
    con = _connect(db_path)
    try:
        con.execute("INSERT INTO runs (id, run_dir) VALUES (?, ?)",
                    (CLOSE_RUNS_ID, str(run_dir)))
        con.commit()
    finally:
        con.close()


def _close_bindings(db_path: Path, run_dir: Path) -> dict[str, str]:
    return {
        "MINI_ORK_DB": str(db_path),
        "MINI_ORK_HOME": str(db_path.parent),
        "MINI_ORK_RUN_ID": CLOSE_RUN,
        "MINI_ORK_RUN_DIR": str(run_dir),
    }


def _failure_rows(db_path: Path) -> list[sqlite3.Row]:
    con = _connect(db_path)
    try:
        return con.execute(
            "SELECT failure_id, run_id, workflow_stage, failure_category, error_message "
            "FROM failure_memory ORDER BY occurred_at, failure_id"
        ).fetchall()
    finally:
        con.close()


def test_failed_close_writes_one_failure_memory_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mini_ork.cli.main as main_mod

    db_path = tmp_path / "state.db"
    run_dir = tmp_path / "run-dir"
    _seed_close_db(db_path, run_dir)
    bindings = _close_bindings(db_path, run_dir)
    _fence_env(monkeypatch, **bindings)

    def _fake_impl(inner, root, sink):
        sink["run_id"] = CLOSE_RUN
        sink["_pid_run_dir"] = str(run_dir)
        return 1

    monkeypatch.setattr(main_mod, "_run_lifecycle_impl", _fake_impl)
    with run_context_scope(dict(bindings)):
        rc = main_mod._run_lifecycle(["unit-close"], root=str(tmp_path))

    assert rc == 1
    con = _connect(db_path)
    try:
        status = con.execute(
            "SELECT status FROM task_runs WHERE id = ?", (CLOSE_RUN,)
        ).fetchone()[0]
    finally:
        con.close()
    assert status == "failed"

    rows = _failure_rows(db_path)
    assert len(rows) == 1, f"expected exactly one failure_memory row, got {rows}"
    assert rows[0]["run_id"] == CLOSE_RUNS_ID
    text = " | ".join(str(rows[0][k]) for k in
                      ("workflow_stage", "failure_category", "error_message"))
    assert "spec_task_class" in text, text
    assert "spec-recipe" in text, text
    assert "researcher-one" in text, (
        f"must name the FIRST failed node, not implementer-two: {text!r}"
    )


def test_failed_close_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-close of an already-failed run must not stack a second row."""
    import mini_ork.cli.main as main_mod

    db_path = tmp_path / "state.db"
    run_dir = tmp_path / "run-dir"
    _seed_close_db(db_path, run_dir)
    bindings = _close_bindings(db_path, run_dir)
    _fence_env(monkeypatch, **bindings)

    def _fake_impl(inner, root, sink):
        sink["run_id"] = CLOSE_RUN
        sink["_pid_run_dir"] = str(run_dir)
        return 1

    monkeypatch.setattr(main_mod, "_run_lifecycle_impl", _fake_impl)
    with run_context_scope(dict(bindings)):
        main_mod._run_lifecycle(["unit-close"], root=str(tmp_path))
        main_mod._close_run_record(
            {"run_id": CLOSE_RUN, "_pid_run_dir": str(run_dir)}, crashed=False, rc=1
        )

    assert len(_failure_rows(db_path)) == 1


def test_failed_close_survives_a_memory_writer_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mini_ork.cli.main as main_mod
    import mini_ork.memory.store as store_mod

    db_path = tmp_path / "state.db"
    run_dir = tmp_path / "run-dir"
    _seed_close_db(db_path, run_dir)
    bindings = _close_bindings(db_path, run_dir)
    _fence_env(monkeypatch, **bindings)

    def _broken_write_failure(*args, **kwargs):
        raise RuntimeError("unit: failure_memory writer down")

    monkeypatch.setattr(store_mod, "write_failure", _broken_write_failure)
    with run_context_scope(dict(bindings)):
        main_mod._close_run_record(
            {"run_id": CLOSE_RUN, "_pid_run_dir": str(run_dir)}, crashed=False, rc=1
        )

    con = _connect(db_path)
    try:
        status = con.execute(
            "SELECT status FROM task_runs WHERE id = ?", (CLOSE_RUN,)
        ).fetchone()[0]
    finally:
        con.close()
    assert status == "failed"
