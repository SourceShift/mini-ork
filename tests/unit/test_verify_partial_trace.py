"""RBF-15: a partial post-run verify must not be recorded as a reward-1.0 pass.

``mini_ork.cli.verify`` used to map every verdict except ``fail`` to trace
``status="success"`` (and thus reward 1.0), so a run whose test gate failed but
whose other verifiers passed — a ``partial`` verdict — was written into the
learning ledger as a clean pass.

These tests drive ``verify.main()`` end to end against a hermetic tmp
root/home/sqlite db. The ``execution_traces`` table carries the canonical live
status CHECK (db/migrations/0054), which has no ``partial`` member: a status the
schema rejects would be swallowed by ``_safe_trace_write`` and leave the row
stuck at ``running``, so the tests assert a terminal row actually landed.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mini_ork.cli import verify as verify_cli

_TRACE_DDL = """
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id              TEXT PRIMARY KEY,
  run_id                INTEGER,
  workflow_version_id   TEXT,
  agent_version_id      TEXT NOT NULL DEFAULT '',
  task_class            TEXT NOT NULL,
  prompt_version_hash   TEXT NOT NULL DEFAULT '',
  context_bundle_hash   TEXT NOT NULL DEFAULT '',
  tool_calls            TEXT NOT NULL DEFAULT '[]',
  files_read            TEXT NOT NULL DEFAULT '[]',
  files_written         TEXT NOT NULL DEFAULT '[]',
  verifier_output       TEXT NOT NULL DEFAULT '{}',
  reviewer_verdict      TEXT,
  cost_usd              REAL NOT NULL DEFAULT 0.0,
  duration_ms           INTEGER NOT NULL DEFAULT 0,
  final_artifact_ref    TEXT,
  status                TEXT NOT NULL
                        CHECK (status IN ('success','failure','pending','running','vacuous','blocked')),
  created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  process_reward        REAL DEFAULT NULL,
  objective_domain      TEXT NOT NULL DEFAULT 'code-delivery',
  segment               TEXT DEFAULT NULL,
  reward_primary_metric TEXT DEFAULT NULL,
  reward_direction      TEXT NOT NULL DEFAULT 'higher_is_better',
  reward_value          REAL DEFAULT NULL,
  reward_anchor         REAL DEFAULT NULL,
  reward_g              REAL DEFAULT NULL,
  reward_vector_json    TEXT DEFAULT NULL,
  reward_source         TEXT NOT NULL DEFAULT 'verifier@v1',
  validity              TEXT NOT NULL DEFAULT 'valid',
  code_region           TEXT DEFAULT NULL,
  route_source          TEXT DEFAULT NULL,
  route_explore         INTEGER DEFAULT NULL,
  route_score           REAL DEFAULT NULL,
  route_margin          REAL DEFAULT NULL,
  predicted_error       REAL DEFAULT NULL
)
"""

# Never let this test adopt the outer mini-ork run's identity (audit RB-04).
_ENV_DETOX = (
    "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MINI_ORK_TASK_RUN_ID",
    "MINI_ORK_DB", "MINI_ORK_HOME", "MINI_ORK_ROOT", "MINI_ORK_PLAN_PATH",
    "MINI_ORK_TASK_CLASS", "MINI_ORK_DRY_RUN", "MINI_ORK_RECIPE",
    "MINI_ORK_TARGET_REPO", "MINI_ORK_WORKFLOW_VERSION_ID",
    "MO_TARGET_CWD", "MO_MUTATION_REPORT", "MO_MUTATION_TEST_CMD",
    "MO_MUTATION_LOG", "MO_VERIFY_RERUN", "MO_NODE_ID", "MO_NODE_PROMPT_SHA",
    "MO_REWARD_ANCHOR", "MO_PLACEMENT", "MO_REMOTE_PROFILE",
)


def _run_verify(tmp_path, monkeypatch, capsys, scripts, task_class):
    """Run one hermetic ``mini-ork verify``; return (verdict, rc, terminal row)."""
    home = tmp_path / "home"
    vdir = tmp_path / "root" / "verifiers"
    home.mkdir(exist_ok=True)
    vdir.mkdir(parents=True, exist_ok=True)
    for name, body in scripts.items():
        (vdir / name).write_text(body, encoding="utf-8")

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "task_class": task_class,
        "artifact_contract": {"success_verifiers": list(scripts)},
    }), encoding="utf-8")
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}\n", encoding="utf-8")

    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    con.executescript(_TRACE_DDL)
    con.commit()
    con.close()

    for key in _ENV_DETOX:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MO_MUTATION_ADVERSARY", "0")
    monkeypatch.setenv("MO_REWARD_STAMP", "1")

    rc = verify_cli.main(
        [str(artifact), "--plan", str(plan), "--task-class", task_class],
        db=str(db), root=str(tmp_path / "root"),
    )
    out = capsys.readouterr().out
    try:
        verdict = json.loads(out)
    except ValueError:
        import re
        m = re.search(r'"verdict":\s*"([a-z-]+)"', out)
        verdict = {"verdict": m.group(1)} if m else {}

    con = sqlite3.connect(str(db))
    rows = [
        dict(zip(("trace_id", "status", "reward_value", "verifier_output"), r))
        for r in con.execute(
            "SELECT trace_id, status, reward_value, verifier_output "
            "FROM execution_traces WHERE task_class = ?", (task_class,))
    ]
    con.close()

    final = [r for r in rows if r["status"] != "running"]
    assert len(rows) >= 1, "verify wrote no trace at all"
    assert len(final) == 1, (
        f"terminal trace never landed; rows={rows} — the status verify emitted "
        "was rejected by the canonical execution_traces CHECK"
    )
    return verdict, rc, final[0]


def test_partial_verdict_is_not_a_success_and_rewards_below_one(
        tmp_path, monkeypatch, capsys):
    verdict, rc, row = _run_verify(
        tmp_path, monkeypatch, capsys,
        {"vpass.py": 'print("pass evidence")\n',
         "vfail.py": 'import sys\nprint("fail evidence")\nsys.exit(1)\n'},
        task_class="rbf15-partial",
    )
    assert verdict.get("verdict") == "partial", verdict

    assert row["status"] in ("partial", "failure"), (
        f"partial verdict laundered into status {row['status']!r}"
    )
    assert row["status"] != "success"
    assert json.loads(row["verifier_output"]).get("verdict") == "partial"

    assert row["reward_value"] is not None
    assert row["reward_value"] < 1.0
    assert row["reward_value"] >= 0.0

    # Return code semantics are unchanged for callers: partial still exits 0.
    assert rc == 0


def test_pass_verdict_still_success_and_reward_one(tmp_path, monkeypatch, capsys):
    verdict, rc, row = _run_verify(
        tmp_path, monkeypatch, capsys,
        {"vpass.py": 'print("pass evidence")\n'},
        task_class="rbf15-pass",
    )
    assert verdict.get("verdict") == "pass", verdict
    assert row["status"] == "success"
    assert row["reward_value"] == pytest.approx(1.0)
    assert rc == 0


def test_fail_verdict_still_failure_and_reward_zero(tmp_path, monkeypatch, capsys):
    verdict, rc, row = _run_verify(
        tmp_path, monkeypatch, capsys,
        {"vfail.py": 'import sys\nprint("fail evidence")\nsys.exit(1)\n'},
        task_class="rbf15-fail",
    )
    assert verdict.get("verdict") == "fail", verdict
    assert row["status"] == "failure"
    assert row["reward_value"] == pytest.approx(0.0)
    assert rc == 1


def test_vacuous_verdict_keeps_vacuous_status(tmp_path, monkeypatch, capsys):
    verdict, rc, row = _run_verify(
        tmp_path, monkeypatch, capsys, {}, task_class="rbf15-vacuous",
    )
    assert verdict.get("verdict") == "vacuous", verdict
    assert row["status"] == "vacuous"
    assert rc == 0
