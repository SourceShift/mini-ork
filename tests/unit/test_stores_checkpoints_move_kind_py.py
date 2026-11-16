"""``node_attempts.move_kind`` — what KIND of move an attempt was (migration 0093).

The ledger has always answered "did this node run again?". It could not answer
"and why": redone infrastructure work, a revision after feedback, and a
deliberate change of idea were one indistinguishable row, which made
convergence spend and exploration spend the same number.

These tests drive the classification and both writers:

* :func:`mini_ork.stores.checkpoints.classify_move` — the pure rule, including
  the ordering and the fact that ``explore`` is never inferred;
* :func:`mini_ork.stores.checkpoints.append_node_attempt` and
  ``write_checkpoint`` — the single INSERT shape both go through;
* ``mini_ork.cli.execute._make_checkpoint_fn`` — the ``_cp`` closure that
  declares ``recover`` from ``MO_RECOVER_OF``.

Compatibility is a first-class case: a state.db without the column must keep
recording attempts exactly as before, because the INSERT is shared with
``write_checkpoint`` and a column-count error there would cost every
checkpoint, not one label.

Hermetic: every test builds its own tmp state.db and run dir. No network, no
provider credentials, no live mini-ork state.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from mini_ork.cli.execute import _make_checkpoint_fn
from mini_ork.stores.checkpoints import (
    MOVE_EXPLORE,
    MOVE_INITIAL,
    MOVE_KINDS,
    MOVE_RECOVER,
    MOVE_RETRY,
    MOVE_REVISE,
    CheckpointRecord,
    append_node_attempt,
    classify_move,
    write_checkpoint,
)

# The 0050 shape (no move_kind) — what an un-migrated state.db looks like.
_LEGACY_DDL = """
CREATE TABLE node_attempts (
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
"""

# 0093 applied.
_MIGRATED_DDL = _LEGACY_DDL.replace(
    "    initiator             TEXT\n);",
    "    initiator             TEXT,\n    move_kind             TEXT\n);",
)

_SUPPORT_DDL = """
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

_CLEARED_ENV = (
    "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_DB", "MINI_ORK_HOME",
    "MO_RECOVER_OF", "MO_NODE_FINISH_REASON",
)


def _build_db(path, ddl: str) -> str:
    con = sqlite3.connect(str(path))
    con.executescript(ddl + _SUPPORT_DDL)
    con.commit()
    con.close()
    return str(path)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A migrated state.db, an empty run dir, and no ambient run identity."""
    for var in _CLEARED_ENV:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    db = _build_db(home / "state.db", _MIGRATED_DDL)
    monkeypatch.setenv("MO_NODE_GIT_CKPT", "0")
    monkeypatch.setenv("MO_ARTIFACT_BACKEND", "local")
    return {"db": db, "run_dir": str(run_dir), "tmp": str(tmp_path)}


def _rows(db: str, run_id: str, node_id: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(r) for r in con.execute(
                "SELECT attempt_no, result, failure_class, move_kind"
                " FROM node_attempts WHERE run_id = ? AND node_id = ?"
                " ORDER BY id",
                (run_id, node_id),
            ).fetchall()
        ]
    finally:
        con.close()


# --------------------------------------------------------------- classify_move


def test_first_attempt_is_initial_whatever_else_is_true():
    assert classify_move(attempt_no=1) == MOVE_INITIAL
    # Even a retry-policy initiator cannot make a node's first attempt a retry:
    # there was nothing to retry.
    assert classify_move(attempt_no=1, initiator="retry-policy") == MOVE_INITIAL
    assert classify_move(
        attempt_no=1, previous_failure_class="infra_interrupt") == MOVE_INITIAL


def test_a_declared_kind_wins_and_explore_only_arrives_that_way():
    # "explore" has no inference path at all: the ledger cannot tell a new idea
    # from a better patch, so only a dispatcher that chose one may say so.
    assert classify_move(attempt_no=4, declared=MOVE_EXPLORE) == MOVE_EXPLORE
    inferred = {
        classify_move(attempt_no=4),
        classify_move(attempt_no=4, previous_failure_class="verifier_refute"),
        classify_move(attempt_no=4, initiator="retry-policy"),
        classify_move(attempt_no=4, previous_failure_class="infra_interrupt"),
        classify_move(attempt_no=4, initiator="recover:run-1"),
    }
    assert MOVE_EXPLORE not in inferred


def test_an_unknown_declared_kind_falls_back_to_inference():
    # A typo must not be stored as a bucket the metrics cannot name.
    assert classify_move(attempt_no=3, declared="expore") == MOVE_REVISE


@pytest.mark.parametrize("cls", ["provider_limit", "infra_interrupt",
                                 "input_required", "retry_exhausted"])
def test_a_rework_free_previous_class_is_a_retry(cls):
    assert classify_move(attempt_no=2, previous_failure_class=cls) == MOVE_RETRY


def test_a_measured_previous_class_is_a_revise():
    for cls in ("verifier_refute", "output_invalid", "terminal"):
        assert classify_move(
            attempt_no=2, previous_failure_class=cls) == MOVE_REVISE


def test_retry_policy_initiator_without_a_previous_row_is_a_retry():
    assert classify_move(attempt_no=2, initiator="retry-policy") == MOVE_RETRY


def test_recover_initiator_is_a_recover():
    assert classify_move(attempt_no=2, initiator="recover:run-1") == MOVE_RECOVER


def test_vocabulary_is_closed():
    assert MOVE_KINDS == ("initial", "retry", "revise", "recover", "explore")


# ------------------------------------------------------------ append_node_attempt


def test_append_stamps_initial_then_revise_from_the_previous_row(env):
    db, run = env["db"], "run-1"
    assert append_node_attempt(db, run, "impl", status="failure",
                               failure_class="verifier_refute") == 0
    assert append_node_attempt(db, run, "impl", status="success") == 0
    rows = _rows(db, run, "impl")
    assert [r["attempt_no"] for r in rows] == [1, 2]
    assert [r["move_kind"] for r in rows] == [MOVE_INITIAL, MOVE_REVISE]


def test_append_reads_the_previous_rows_infra_class_not_its_own(env):
    db, run = env["db"], "run-2"
    # Attempt 1 dies on infrastructure; attempt 2 is therefore a retry even
    # though attempt 2 itself ends in a measured failure.
    assert append_node_attempt(db, run, "n", status="failure",
                               failure_class="infra_interrupt") == 0
    assert append_node_attempt(db, run, "n", status="failure",
                               failure_class="verifier_refute") == 0
    assert append_node_attempt(db, run, "n", status="failure",
                               failure_class="verifier_refute") == 0
    kinds = [r["move_kind"] for r in _rows(db, run, "n")]
    assert kinds == [MOVE_INITIAL, MOVE_RETRY, MOVE_REVISE]


def test_append_honours_a_declared_explore(env):
    db, run = env["db"], "run-3"
    append_node_attempt(db, run, "n", status="failure",
                        failure_class="verifier_refute")
    assert append_node_attempt(db, run, "n", status="failure",
                               move_kind=MOVE_EXPLORE) == 0
    assert _rows(db, run, "n")[-1]["move_kind"] == MOVE_EXPLORE


def test_append_still_writes_when_the_column_is_absent(tmp_path):
    """An un-migrated ledger keeps recording attempts exactly as before."""
    db = _build_db(tmp_path / "legacy.db", _LEGACY_DDL)
    assert append_node_attempt(db, "run-legacy", "n", status="success") == 0
    assert append_node_attempt(db, "run-legacy", "n", status="failure",
                               failure_class="infra_interrupt") == 0
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT attempt_no, result, failure_class FROM node_attempts"
            " WHERE run_id = ? AND node_id = ? ORDER BY id",
            ("run-legacy", "n"),
        ).fetchall()]
        columns = {r[1] for r in con.execute(
            "PRAGMA table_info(node_attempts)").fetchall()}
    finally:
        con.close()
    assert "move_kind" not in columns
    assert [r["attempt_no"] for r in rows] == [1, 2]
    assert [r["result"] for r in rows] == ["success", "failure"]


# ------------------------------------------------------------- write_checkpoint


def _artifact(env, name="out.txt") -> str:
    path = os.path.join(env["run_dir"], name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("move-kind output\n")
    return path


def _checkpoint_kwargs(env, output: str) -> dict:
    return {
        "status": "success",
        "input_hash": "ih",
        "recipe_version": "spec",
        "config_hash": "ch",
        "artifact_paths": [output],
        "run_dir": env["run_dir"],
        "started_at": 1,
        "ended_at": 2,
    }


def test_write_checkpoint_stamps_initial(env):
    out = _artifact(env)
    assert write_checkpoint(env["db"], "run-cp", "n",
                            **_checkpoint_kwargs(env, out)) == 0
    assert _rows(env["db"], "run-cp", "n")[0]["move_kind"] == MOVE_INITIAL


def test_write_checkpoint_honours_the_record_field(env):
    out = _artifact(env)
    rec = CheckpointRecord(
        **_checkpoint_kwargs(env, out), move_kind=MOVE_EXPLORE)
    assert write_checkpoint(env["db"], "run-cp2", "n", record=rec) == 0
    assert _rows(env["db"], "run-cp2", "n")[0]["move_kind"] == MOVE_EXPLORE


# -------------------------------------------------- _cp closure (production seam)


def _make_cp(env, run_id: str):
    return _make_checkpoint_fn(
        env["db"], run_id, env["run_dir"], "move-kind-recipe", "spec")


def test_cp_stamps_initial_and_retry_without_a_recover_env(env, monkeypatch):
    monkeypatch.delenv("MO_RECOVER_OF", raising=False)
    cp = _make_cp(env, "run-cp-a")
    cp("impl", "failure", "implementer", finish_reason="provider_limit")
    cp("impl", "failure", "implementer", finish_reason="verifier_refute")
    assert [r["move_kind"] for r in _rows(env["db"], "run-cp-a", "impl")] == [
        MOVE_INITIAL, MOVE_RETRY]


def test_cp_declares_recover_for_a_resumed_run(env, monkeypatch):
    """A recovering process re-executes interrupted work, first attempts
    included: that is a property of the process, not of the ledger."""
    monkeypatch.setenv("MO_RECOVER_OF", "run-original")
    cp = _make_cp(env, "run-cp-b")
    cp("impl", "failure", "implementer", finish_reason="infra_interrupt")
    assert _rows(env["db"], "run-cp-b", "impl")[0]["move_kind"] == MOVE_RECOVER


def test_cp_success_path_stamps_through_write_checkpoint(env, monkeypatch):
    monkeypatch.delenv("MO_RECOVER_OF", raising=False)
    cp = _make_cp(env, "run-cp-c")
    out = _artifact(env, "node_out.txt")
    cp("impl", "success", "implementer", out)
    rows = _rows(env["db"], "run-cp-c", "impl")
    assert len(rows) == 1
    assert rows[0]["result"] == "success"
    assert rows[0]["move_kind"] == MOVE_INITIAL
