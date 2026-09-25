"""Unit tests: node lifecycle events wired into ``dispatch_node``.

Verifies that ``dispatch_node`` emits exactly one ``node_start`` before
dispatch and one ``node_end`` at the ``trace()`` completion seam, writing to
the run's db via the keyword-only ``db=`` override. Hermetic: a throwaway
state.db seeded by ``db/init.sh``, no lane, no network, no real run.

Kept independent of the ported writer's mirror tests
(``test_mo_node_events_py.py``), which pin the ported semantics and must stay
green unmodified.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import mini_ork.cli.execute as ex
from mini_ork.observability import node_events as py

INIT_SH = REPO / "db" / "init.sh"


def _seed_db(home: Path) -> str:
    """Materialize a fresh state.db under ``home`` via db/init.sh."""
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _events(db: str, run_id: str) -> list[dict]:
    """Run's node_start/node_end rows, oldest first."""
    if not db or not os.path.isfile(db):
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT event_type, payload_json FROM run_events "
            "WHERE run_id=? AND event_type IN ('node_start', 'node_end') "
            "ORDER BY created_at, event_id",
            (run_id,),
        ).fetchall()
    finally:
        con.close()
    return [{"event_type": r[0], "payload": json.loads(r[1])} for r in rows]


def _tracing_handler(captured, *, verdict="pass", finish_reason="done", rc=0,
                     artifact="/tmp/art.md"):
    """A registered node handler that funnels through the trace() seam."""
    def handler(ctx):
        captured["lane"] = ctx.lane
        captured["node_id"] = ctx.node_id
        ctx.trace(ctx.node_id, "success" if rc == 0 else "failure",
                  ctx.node_type, artifact, verdict, finish_reason)
        return rc, finish_reason
    return handler


def _dispatch(tmp_path, monkeypatch, db, run_id, node_id, node_type, *,
              model_lane="", handler=None):
    """Drive dispatch_node once with a registered node handler."""
    for key in ("MINI_ORK_RUN_DIR", "MO_NODE_ID", "MO_RESUME_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    rd = tmp_path / "run"
    rd.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}))
    fields = (node_id, node_type, f"do {node_id}", "", "serial", "", model_lane, "")
    if handler is not None:
        ex.register_node_handler(node_type, handler)
    try:
        return ex.dispatch_node(
            fields, root=os.getcwd(), run_dir=str(rd), plan_path=str(plan),
            task_class="generic", db=db, run_id=run_id,
            dispatch_fn=lambda *a: (0, "done"),
        )
    finally:
        if handler is not None:
            ex.NODE_HANDLER_REGISTRY.pop(node_type, None)


def test_dispatch_node_emits_start_and_end(tmp_path, monkeypatch):
    """A dispatched node writes exactly one node_start and one node_end row."""
    home = tmp_path / "home"
    home.mkdir()
    db = _seed_db(home)
    captured: dict = {}
    rc, fr = _dispatch(
        tmp_path, monkeypatch, db, "run-a", "n-a", "node_evt_happy",
        handler=_tracing_handler(captured),
    )
    assert (rc, fr) == (0, "done")
    rows = _events(db, "run-a")
    starts = [r for r in rows if r["event_type"] == "node_start"]
    ends = [r for r in rows if r["event_type"] == "node_end"]
    assert len(starts) == 1, rows
    assert len(ends) == 1, rows
    start = starts[0]["payload"]
    assert start["node_id"] == "n-a"
    assert start["node_type"] == "node_evt_happy"
    assert start["model_lane"] == captured["lane"]
    end = ends[0]["payload"]
    assert end["verdict"] == "pass"
    assert end["finish_reason"] == "done"
    assert end["artifact_path"] == "/tmp/art.md"
    assert end["duration_ms"] >= 0


def test_dispatch_node_end_carries_duration_and_verdict(tmp_path, monkeypatch):
    """The node_end payload carries the handler's verdict/finish_reason and a
    non-negative duration_ms computed from the recorded start."""
    home = tmp_path / "home"
    home.mkdir()
    db = _seed_db(home)
    captured: dict = {}
    rc, fr = _dispatch(
        tmp_path, monkeypatch, db, "run-b", "n-b", "node_evt_verdict",
        handler=_tracing_handler(captured, verdict="needs_revision",
                                 finish_reason="verdict_fail", rc=0,
                                 artifact="/tmp/rev.md"),
    )
    assert (rc, fr) == (0, "verdict_fail")
    rows = _events(db, "run-b")
    ends = [r for r in rows if r["event_type"] == "node_end"]
    assert len(ends) == 1
    payload = ends[0]["payload"]
    assert payload["verdict"] == "needs_revision"
    assert payload["finish_reason"] == "verdict_fail"
    assert payload["artifact_path"] == "/tmp/rev.md"
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0


def test_dispatch_node_failure_still_emits_end(tmp_path, monkeypatch):
    """A failing dispatch (rc != 0) still records a node_end with a start."""
    home = tmp_path / "home"
    home.mkdir()
    db = _seed_db(home)
    captured: dict = {}
    rc, fr = _dispatch(
        tmp_path, monkeypatch, db, "run-c", "n-c", "node_evt_fail",
        handler=_tracing_handler(captured, verdict="", finish_reason="error",
                                 rc=1),
    )
    assert (rc, fr) == (1, "error")
    rows = _events(db, "run-c")
    starts = [r for r in rows if r["event_type"] == "node_start"]
    ends = [r for r in rows if r["event_type"] == "node_end"]
    assert len(starts) == 1, rows
    assert len(ends) == 1, rows
    assert ends[0]["payload"]["finish_reason"] == "error"


def test_dispatch_node_start_records_model_lane(tmp_path, monkeypatch):
    """The model lane passed to the node appears in the node_start payload."""
    home = tmp_path / "home"
    home.mkdir()
    db = _seed_db(home)
    captured: dict = {}
    _dispatch(
        tmp_path, monkeypatch, db, "run-d", "n-d", "node_evt_lane",
        model_lane="opus", handler=_tracing_handler(captured),
    )
    rows = _events(db, "run-d")
    starts = [r for r in rows if r["event_type"] == "node_start"]
    assert len(starts) == 1
    assert starts[0]["payload"]["model_lane"] == captured["lane"]
    assert captured["lane"]


def test_explicit_db_precedence(tmp_path, monkeypatch):
    """mo_node_start(..., db=<explicit>) writes to that path even when
    MINI_ORK_DB/MINI_ORK_HOME point elsewhere."""
    target_home = tmp_path / "target"
    target_home.mkdir()
    env_home = tmp_path / "env"
    env_home.mkdir()
    db_target = _seed_db(target_home)
    db_env = _seed_db(env_home)
    monkeypatch.setenv("MINI_ORK_DB", db_env)
    monkeypatch.setenv("MINI_ORK_HOME", str(env_home))

    py.mo_node_start("run-x", "n-x", "researcher", "opus", db=db_target)

    target_rows = _events(db_target, "run-x")
    env_rows = _events(db_env, "run-x")
    assert len(target_rows) == 1
    assert len(env_rows) == 0
    assert target_rows[0]["event_type"] == "node_start"
    assert target_rows[0]["payload"]["model_lane"] == "opus"


def test_dispatch_node_fail_silent_missing_db(tmp_path, monkeypatch):
    """With a nonexistent db path, dispatch_node still returns its normal
    (rc, finish_reason) and writes nothing (fail-silent observability)."""
    missing = str(tmp_path / "nope" / "state.db")
    captured: dict = {}
    rc, fr = _dispatch(
        tmp_path, monkeypatch, missing, "run-f", "n-f", "node_evt_silent",
        handler=_tracing_handler(captured),
    )
    assert (rc, fr) == (0, "done")
    assert not os.path.exists(missing)
