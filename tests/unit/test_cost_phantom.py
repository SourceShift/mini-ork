"""RB-32a: no phantom planner charge; the cost-pause meter includes planning.

Each test drives ``mini_ork.cli.plan.main`` with a stubbed dispatch seam against
a hermetic tmp sqlite DB and a tmp ``MINI_ORK_HOME`` — no network, no provider
credentials, no live-DB writes.
"""
from __future__ import annotations

import json
import sqlite3
import time
import types
import uuid
from pathlib import Path

import pytest

from mini_ork.cli import plan as plan_mod
from mini_ork.dispatch import cost_pause

REPO_ROOT = Path(plan_mod.__file__).resolve().parents[2]

VALID_PLAN = {
    "objective": "Ship the RBF-32a cost fix",
    "assumptions": [],
    "decomposition": [
        {"id": "impl", "description": "edit the two modules", "node_type": "implementer",
         "depends_on": []},
        {"id": "ver", "description": "run the unit gate", "node_type": "verifier",
         "depends_on": ["impl"]},
    ],
    "dependencies": [{"from": "impl", "to": "ver"}],
    "risk_notes": [],
    "artifact_contract": {"outputs": ["out.txt"],
                          "success_verifiers": ["verifiers/test.py"]},
    "verifier_contract": {"checks": [{"id": "c1", "description": "the unit gate passes"}]},
}

_TASK_RUNS_DDL = """
CREATE TABLE task_runs (
  id                TEXT PRIMARY KEY,
  task_class        TEXT NOT NULL,
  recipe            TEXT,
  kickoff_path      TEXT NOT NULL,
  plan_path         TEXT,
  plan_hash         TEXT,
  status            TEXT NOT NULL DEFAULT 'classified',
  verdict           TEXT,
  cost_usd          REAL NOT NULL DEFAULT 0,
  duration_ms       INTEGER NOT NULL DEFAULT 0,
  trace_id          TEXT,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,
  ended_at          INTEGER,
  notes             TEXT
);
"""

_LLM_CALLS_DDL = """
CREATE TABLE llm_calls (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  provider        TEXT NOT NULL,
  model_id        TEXT NOT NULL,
  tier            TEXT NOT NULL,
  feature_name    TEXT NOT NULL,
  actor           TEXT,
  epic_id         TEXT,
  dispatch_id     INTEGER,
  run_id          TEXT,
  iter            INTEGER,
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  total_tokens    INTEGER NOT NULL DEFAULT 0,
  cost_usd        REAL NOT NULL DEFAULT 0,
  duration_ms     INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'success',
  finish_reason   TEXT,
  error_message   TEXT,
  traceparent     TEXT,
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def _connect(db):
    return sqlite3.connect(db)


def _insert_run(env, cost_usd=0.0):
    con = _connect(env.db)
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, cost_usd,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (env.run_id, "generic", str(env.kickoff), "classified", cost_usd, now, now),
    )
    con.commit()
    con.close()


def _insert_call(env, run_id, cost_usd):
    con = _connect(env.db)
    con.execute(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, run_id,"
        " cost_usd, status) VALUES (?,?,?,?,?,?,?)",
        ("anthropic", "claude-stub", "default", "mini-orch:planner",
         run_id, cost_usd, "success"),
    )
    con.commit()
    con.close()


def _run_cost(env):
    con = _connect(env.db)
    row = con.execute("SELECT cost_usd FROM task_runs WHERE id = ?",
                      (env.run_id,)).fetchone()
    con.close()
    assert row is not None
    return float(row[0])


def _run_plan(env, replies):
    pending = list(replies)

    def dispatch(task_class, node_type, prompt):
        assert node_type == "planner"
        return pending.pop(0)

    rc = plan_mod.main(
        [str(env.kickoff), "--task-class", "generic", "--out", str(env.out)],
        root=str(REPO_ROOT),
        dispatch=dispatch,
    )
    assert not pending
    return rc


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "mini-ork-home"
    home.mkdir()
    db = home / "state.db"
    con = _connect(db)
    con.execute(_TASK_RUNS_DDL)
    con.execute(_LLM_CALLS_DDL)
    con.commit()
    con.close()
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# kickoff\n\nDo the thing.\n", encoding="utf-8")
    e = types.SimpleNamespace(
        home=home, db=db, kickoff=kickoff, out=tmp_path / "plan.json",
        run_id=f"run-cost-{uuid.uuid4().hex[:12]}",
    )
    for key in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_WORKFLOW",
        "MO_PLAN_ARTIFACT", "MO_GIVEN_PLAN", "MINI_ORK_PROFILE_PATH",
        "MINI_ORK_TASK_CLASS", "MO_STATIC_RECIPE_PLAN",
        "MO_FORCE_RECIPE_FALLBACK_PLAN", "MO_PLAN_DETERMINISTIC_FALLBACK",
        "MINI_ORK_DRY_RUN", "MO_PLAN_MAX_REPAIRS", "MO_PROFILE_REQUIRE_ANSWERS",
        "MO_AUTO_ANSWER_PROFILE", "MINI_ORK_PLAN_CONFIDENCE_FLOOR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_RUN_ID", e.run_id)
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO_ROOT))
    monkeypatch.setenv("MO_INJECT_LEARNINGS", "0")
    monkeypatch.setenv("MINI_ORK_PROFILE_GATE", "0")
    monkeypatch.setenv("MO_PAUSE_EVERY_USD", "25")
    return e


def test_charge_cost_helper_is_deleted():
    """The flat +$0.05 charge is gone, not merely disabled."""
    src = Path(plan_mod.__file__).read_text(encoding="utf-8")
    assert "_charge_cost" not in src
    assert not hasattr(plan_mod, "_charge_cost")


def test_rc43_zero_ledger_spend_keeps_cost_zero(env):
    """A 0-token refusal must not bill the run."""
    _insert_run(env)
    _insert_call(env, "run-cost-decoy", 99.0)
    rc = _run_plan(env, [(43, "provider spawn failed: no such executable")])
    assert rc == 1
    assert _run_cost(env) == 0.0


def test_repair_dispatch_charges_nothing(env):
    """The repair redispatch must not add a phantom charge either."""
    _insert_run(env)
    rc = _run_plan(env, [(0, "sorry, not JSON"), (0, json.dumps(VALID_PLAN))])
    assert rc == 0
    assert _run_cost(env) == 0.0


def test_successful_plan_cost_equals_ledger_sum(env):
    """A successful plan's cost equals this run's llm_calls sum — no +0.05."""
    _insert_run(env)
    _insert_call(env, env.run_id, 0.23)
    _insert_call(env, env.run_id, 0.14)
    _insert_call(env, "run-cost-decoy", 99.0)
    assert _run_plan(env, [(0, json.dumps(VALID_PLAN))]) == 0
    assert abs(_run_cost(env) - 0.37) <= 1e-6


def test_meter_includes_planner_spend(env, monkeypatch):
    """Planning seeds/raises .cost-spent so the pause math sees it."""
    monkeypatch.setenv("MO_PAUSE_EVERY_USD", "1.0")
    _insert_run(env)
    meter = env.home / "runs" / env.run_id
    meter.mkdir(parents=True)
    (meter / ".cost-spent").write_text("0.20\n", encoding="utf-8")
    _insert_call(env, env.run_id, 0.59)
    _insert_call(env, "run-cost-decoy", 99.0)
    assert _run_plan(env, [(0, json.dumps(VALID_PLAN))]) == 0

    st = cost_pause.status(env.run_id)
    assert st["sentinel_path"].startswith(str(env.home))
    assert st["spent_usd"] >= 0.59 - 1e-9
    assert st["spent_usd"] < 1.0

    # 0.59 on the meter + $0.45 crosses the $1 window; an unseeded meter
    # (prev=0) would not cross anything.
    assert cost_pause.check(env.run_id, 0.45) == 2
    assert cost_pause.status(env.run_id)["paused"] is True
