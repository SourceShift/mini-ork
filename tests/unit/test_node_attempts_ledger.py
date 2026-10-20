"""``node_attempts`` records every attempt with its number, result and cost.

Kickoff rbf-attempt-ledger (RUN-BUGS RB-29 + RB-33 retried-node case).

Before this change the ledger held one row per *successful* node
(attempt_no always 1, cost always NULL), and a retried attempt's
``node_end`` had no matching ``node_start`` and reported ``duration_ms: 0``
(run-1792091737-99865 implementer: ~38 min recorded as 0). These tests drive
the two production seams the kickoff names:

* ``mini_ork.cli.execute._make_checkpoint_fn`` — the ``_cp`` closure the
  ``trace()`` wrapper calls at every node end;
* ``mini_ork.cli.execute_handlers.dispatch_node`` — the per-attempt
  ``mo_node_start`` re-arm and the ``node_end`` duration;
* ``mini_ork.recovery.retry_hint`` — the reader that must now find a
  ``failure`` row.

Hermetic: every test builds its own tmp sqlite state.db (schema from
migrations 0050 + 0002) and a plain tmp run dir. No network, no provider
credentials, no live mini-ork state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from mini_ork.cli.execute import _make_checkpoint_fn
from mini_ork.cli.execute_handlers import (
    NODE_HANDLER_REGISTRY,
    dispatch_node,
    register_node_handler,
)
from mini_ork.recovery.retry_hint import (
    _failure_class_for_any,
    _failure_class_for_node,
)

_PROBE_NODE_TYPE = "ledger_probe_node"

_DDL = """
CREATE TABLE IF NOT EXISTS node_attempts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt_no            INTEGER NOT NULL,
    node_type             TEXT,
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER NOT NULL,
    result                TEXT    NOT NULL CHECK (result IN ('success','failure','skipped','error')),
    failure_class         TEXT,
    checkpoint_used       INTEGER NOT NULL DEFAULT 0,
    checkpoint_produced   INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL,
    provider_session_id   TEXT,
    initiator             TEXT
);
CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt               INTEGER NOT NULL DEFAULT 1,
    status                TEXT    NOT NULL CHECK (status IN ('success','failure','skipped')),
    input_hash            TEXT    NOT NULL,
    recipe_version        TEXT    NOT NULL,
    config_hash           TEXT    NOT NULL,
    artifact_manifest_json TEXT   NOT NULL,
    session_ref           TEXT,
    failure_class         TEXT,
    created_at            INTEGER NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS run_events (
  event_id       TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL,
  parent_run_id  TEXT,
  event_type     TEXT NOT NULL,
  payload_json   TEXT NOT NULL DEFAULT '{}',
  created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  finish_reason  TEXT,
  last_heartbeat_at INTEGER
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  provider        TEXT NOT NULL,
  model_id        TEXT NOT NULL,
  tier            TEXT NOT NULL,
  feature_name    TEXT NOT NULL,
  actor           TEXT,
  run_id          INTEGER,
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  total_tokens    INTEGER NOT NULL DEFAULT 0,
  cost_usd        REAL NOT NULL DEFAULT 0,
  duration_ms     INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL CHECK (status IN ('success','failed')),
  finish_reason   TEXT,
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

# A nested mini-ork call inside these tests must not adopt the live run's
# identity (audit RB-04) or reach the operator's real home/db.
_RUN_IDENTITY_ENV = (
    "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_DB", "MINI_ORK_HOME",
    "MINI_ORK_RECOVERY_CLOSURE", "MINI_ORK_RECOVERY_FROM",
    "MINI_ORK_LEASE_TOKEN", "MINI_ORK_LEASE_RUN_ID",
)


class Env:
    def __init__(self, home: str, db: str, run_dir: str, tmp: str):
        self.home = home
        self.db = db
        self.run_dir = run_dir
        self.tmp = tmp


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "state.db"
    con = sqlite3.connect(str(db_path))
    con.executescript(_DDL)
    con.commit()
    con.close()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for var in _RUN_IDENTITY_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("MO_NODE_FINISH_REASON", raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db_path))
    # Keep git-touching hooks away from any real checkout.
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))
    monkeypatch.setenv("MO_NODE_GIT_CKPT", "0")
    monkeypatch.setenv("MO_ARTIFACT_BACKEND", "local")
    return Env(str(home), str(db_path), str(run_dir), str(tmp_path))


def _fetch(db: str, sql: str, params=()) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _make_cp(env: Env, run_id: str):
    return _make_checkpoint_fn(env.db, run_id, env.run_dir, "ledger-recipe", "spec")


def _artifact(env: Env, name: str = "out.txt") -> str:
    path = os.path.join(env.run_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("ledger output\n")
    return path


def _attempts(env: Env, run_id: str, node_id: str) -> list[dict]:
    return _fetch(
        env.db,
        "SELECT attempt_no, result, failure_class, checkpoint_produced, cost_usd "
        "FROM node_attempts WHERE run_id = ? AND node_id = ? ORDER BY attempt_no",
        (run_id, node_id),
    )


def _dispatch_flaky(env: Env, run_id: str, node_id: str, *, fail_times: int) -> None:
    """One dispatch_node call: the stub node fails ``fail_times`` attempts
    (retryable ``provider_limit``) then succeeds. Sleeps per attempt so the
    node_end durations are observable."""
    state = {"calls": 0}

    def handler(ctx):
        state["calls"] += 1
        time.sleep(0.05)
        out = os.path.join(ctx.run_dir_eff, "out.txt")
        if state["calls"] <= fail_times:
            ctx.trace(ctx.node_id, "failure", ctx.node_type,
                      finish_reason="provider_limit")
            return 1, "provider_limit"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("ledger output\n")
        ctx.trace(ctx.node_id, "success", ctx.node_type, output_file=out,
                  finish_reason="done")
        return 0, "done"

    register_node_handler(_PROBE_NODE_TYPE, handler)
    try:
        fields = [node_id, _PROBE_NODE_TYPE, "ledger probe", "", "", "", "", ""]
        rc, finish_reason = dispatch_node(
            fields,
            root=env.tmp,
            run_dir=env.run_dir,
            plan_path="",
            task_class="spec",
            db=env.db,
            run_id=run_id,
            dispatch_fn=lambda *a, **k: (0, ""),
            recipe="",
            workflow="",
            checkpoint_fn=_make_cp(env, run_id),
        )
        assert (rc, finish_reason) == (0, "done"), (
            f"stub node should end done, got ({rc}, {finish_reason})")
    finally:
        NODE_HANDLER_REGISTRY.pop(_PROBE_NODE_TYPE, None)


def _events(env: Env, run_id: str, node_id: str) -> list[dict]:
    rows = _fetch(
        env.db,
        "SELECT event_type, payload_json FROM run_events "
        "WHERE run_id = ? ORDER BY rowid",
        (run_id,),
    )
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        if payload.get("node_id") == node_id:
            out.append({"event_type": row["event_type"], "payload": payload})
    return out


# ── ledger rows ─────────────────────────────────────────────────────────────


def test_failure_then_success_two_rows(env, monkeypatch):
    cp = _make_cp(env, "ledger-fs")
    monkeypatch.setenv("MO_NODE_FINISH_REASON", "provider_limit")
    cp("implementer", "failure", "implementer")
    monkeypatch.setenv("MO_NODE_FINISH_REASON", "done")
    cp("implementer", "success", "implementer", output_file=_artifact(env))

    rows = _attempts(env, "ledger-fs", "implementer")
    assert [(r["attempt_no"], r["result"]) for r in rows] == [
        (1, "failure"), (2, "success"),
    ], rows
    assert rows[0]["failure_class"] == "provider_limit", rows[0]
    assert rows[0]["checkpoint_produced"] == 0, rows[0]
    assert rows[1]["checkpoint_produced"] == 1, rows[1]


def test_every_end_writes_a_row_including_skipped_and_error(env):
    cp = _make_cp(env, "ledger-vocab")
    cp("probe", "failure", "researcher")
    cp("probe", "error", "researcher")
    cp("probe", "skipped", "researcher")
    cp("probe", "success", "researcher", output_file=_artifact(env))

    rows = _attempts(env, "ledger-vocab", "probe")
    assert [(r["attempt_no"], r["result"]) for r in rows] == [
        (1, "failure"), (2, "error"), (3, "skipped"), (4, "success"),
    ], rows


def test_attempt_no_is_one_plus_max_not_count(env):
    run_id = "ledger-gap"
    con = sqlite3.connect(env.db)
    for attempt_no in (1, 3):  # COUNT+1 would say 3; MAX+1 correctly says 4
        con.execute(
            "INSERT INTO node_attempts (run_id, node_id, attempt_no, node_type,"
            " started_at, ended_at, result) VALUES (?, 'gap', ?, 'researcher',"
            " 1, 2, 'failure')",
            (run_id, attempt_no),
        )
    con.commit()
    con.close()

    _make_cp(env, run_id)("gap", "failure", "researcher")

    rows = _attempts(env, run_id, "gap")
    assert [r["attempt_no"] for r in rows] == [1, 3, 4], rows


def test_cost_usd_reflects_llm_calls_since_attempt_start(env):
    run_id = "ledger-cost"
    cp = _make_cp(env, run_id)
    con = sqlite3.connect(env.db)
    for cost in (0.05, 0.07):
        con.execute(
            "INSERT INTO llm_calls (provider, model_id, tier, feature_name,"
            " actor, run_id, total_tokens, cost_usd, duration_ms, status,"
            " metadata_json) VALUES ('anthropic', 'spec-model', 'default',"
            " 'mini-ork:implementer', 'implementer', ?, 100, ?, 500,"
            " 'success', '{}')",
            (run_id, cost),
        )
    con.commit()
    con.close()

    cp("implementer", "success", "implementer", output_file=_artifact(env))

    rows = _attempts(env, run_id, "implementer")
    assert rows and rows[-1]["cost_usd"] is not None and rows[-1]["cost_usd"] > 0, rows


# ── retried attempts / dispatch loop ────────────────────────────────────────


def test_dispatch_failure_then_success_writes_two_rows(env):
    _dispatch_flaky(env, "ledger-disp", "flaky", fail_times=1)

    rows = _attempts(env, "ledger-disp", "flaky")
    assert [(r["attempt_no"], r["result"]) for r in rows] == [
        (1, "failure"), (2, "success"),
    ], rows
    assert rows[0]["failure_class"] == "provider_limit", rows[0]


def test_second_dispatch_in_revise_round_continues_at_three(env):
    _dispatch_flaky(env, "ledger-revise", "flaky", fail_times=1)
    # Revise round: the same node is dispatched again and succeeds first try.
    _dispatch_flaky(env, "ledger-revise", "flaky", fail_times=0)

    rows = _attempts(env, "ledger-revise", "flaky")
    assert [r["attempt_no"] for r in rows] == [1, 2, 3], rows
    assert rows[-1]["result"] == "success", rows[-1]


def test_retried_attempt_has_own_node_start_and_nonzero_duration(env):
    _dispatch_flaky(env, "ledger-events", "flaky", fail_times=1)

    events = _events(env, "ledger-events", "flaky")
    sequence = [
        e["event_type"] for e in events
        if e["event_type"] in ("node_start", "node_end")
    ]
    assert sequence == ["node_start", "node_end", "node_start", "node_end"], (
        f"one start/end pair per attempt, got {sequence}")
    for event in events:
        if event["event_type"] != "node_end":
            continue
        duration = event["payload"].get("duration_ms")
        assert isinstance(duration, int) and duration > 0, (
            f"every node_end needs a real duration, got {duration!r}")


# ── reader side (RB-33): recovery now finds the failure row ─────────────────


def test_retry_hint_finds_failure_row(env, monkeypatch):
    run_id = "ledger-hint"
    monkeypatch.setenv("MO_NODE_FINISH_REASON", "provider_limit")
    _make_cp(env, run_id)("implementer", "failure", "implementer")

    fc_any, node = _failure_class_for_any(env.home, run_id)
    assert fc_any == "provider_limit" and node == "implementer", (fc_any, node)

    fc_node, _tail = _failure_class_for_node(env.home, run_id, "implementer")
    assert fc_node == "provider_limit", fc_node
