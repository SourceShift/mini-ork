"""Verifier node → ``verifier_results`` wire.

Before this wire the ``verifier_results`` table (migration 0025) was dead
schema: the writer ``gates.verifier_rubric.verifier_result_record`` existed and
was unit-tested, the consumer ``gates.abstain_gate`` read it as the verifier's
labelled calibration history, and nothing on the run path ever wrote a row —
while 8k+ ``execution_traces`` claimed ``reward_source='verifier@v1'``. These
tests drive a real verifier node through ``execute.dispatch_node`` and assert
the row lands with the right ``verdict`` and identity.

Three outcomes:
  * pass  — the verifier script exits 0 → ``verdict='pass'``;
  * fail  — the verifier script exits non-zero → ``verdict='fail'``;
  * vacuous — the run-local required artifact is missing so the verifier never
    runs → ``verdict='vacuous'`` (the DDL's "produced nothing" value).
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
from mini_ork.cli import execute as ex  # noqa: E402


def _seed(tmp: Path) -> str:
    home = tmp / "db" / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,cost_usd,created_at,"
                "updated_at) VALUES ('r1','x','v1','k.md','executing',0,strftime('%s','now'),strftime('%s','now'))")
    con.commit()
    con.close()
    return db


def _fields(node_id: str, vref: str = ""):
    return (node_id, "verifier", f"do {node_id}", "", "serial", vref, "verifier", "")


def _rows(db: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT verdict, verifier_name, scored_axes_json FROM verifier_results "
            "WHERE run_id='r1' ORDER BY created_at"
        ).fetchall()
    finally:
        con.close()


def _pre_impl_run(tmp: Path, db: str, monkeypatch, node_id: str, vref: str,
                  run_verifier) -> tuple[int, str]:
    """Drive a pre-implementation verifier node (its artifact guard is skipped).

    Mirrors ``test_verifier_nodes_run._self_migrate``: the self-migrate recipe's
    ``pre_retirement_parity`` node runs before any implementer, so the hollow-run
    guard is bypassed and the node reaches its verifier script.
    """
    rd = tmp / "run"
    rd.mkdir(exist_ok=True)
    # A scored rubric next to the run so scored_axes_json is populated.
    (rd / "rubric.json").write_text(json.dumps(
        {"pass": True, "score": 1, "items": [{"label": "L", "verdict": "PASS", "note": "n"}]}))
    plan = tmp / "plan.json"
    plan.write_text(json.dumps(
        {"objective": "o", "artifact_contract": {"outputs": [str(rd / "self-migrate.diff")]}}))
    monkeypatch.setattr(ex, "_run_verifier_ref", run_verifier)
    return ex.dispatch_node(_fields(node_id, vref), root=str(REPO), run_dir=str(rd),
                            plan_path=str(plan), task_class="self_migrate", db=db, run_id="r1",
                            dispatch_fn=lambda *a: (0, ""), recipe="self-migrate",
                            workflow=str(REPO / "recipes" / "self-migrate" / "workflow.yaml"))


def test_passing_verifier_records_a_pass_row(tmp_path, monkeypatch) -> None:
    db = _seed(tmp_path)

    def passing(script, evidence_path, **_kw):
        Path(evidence_path).write_text('{"pass":true}\n')
        return 0

    rc, fr = _pre_impl_run(tmp_path, db, monkeypatch, "pre_retirement_parity",
                           "verifiers/pre-retirement-parity.py", passing)
    assert (rc, fr) == (0, "done")
    rows = _rows(db)
    assert len(rows) == 1
    verdict, name, axes = rows[0]
    assert verdict == "pass"
    # verifier_name is the verifier_ref stem — the identity the abstain gate groups by.
    assert name == "pre-retirement-parity"
    # The scored rubric axes ride along so the verdict is auditable.
    assert axes is not None and json.loads(axes)[0]["label"] == "L"


def test_failing_verifier_records_a_fail_row(tmp_path, monkeypatch) -> None:
    db = _seed(tmp_path)

    def failing(script, evidence_path, **_kw):
        Path(evidence_path).write_text('{"pass":false}\n')
        return 1

    rc, fr = _pre_impl_run(tmp_path, db, monkeypatch, "pre_retirement_parity",
                           "verifiers/pre-retirement-parity.py", failing)
    assert (rc, fr) == (1, "error")
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][0] == "fail"


def test_verifier_that_never_runs_records_vacuous(tmp_path) -> None:
    db = _seed(tmp_path)
    rd = tmp_path / "run"
    rd.mkdir()
    plan = tmp_path / "plan.json"
    # framework_edit + a required run-local artifact that is absent → the
    # hollow-run guard fails the node before the verifier script runs.
    plan.write_text(json.dumps(
        {"objective": "o", "artifact_contract": {"required_artifacts": [str(rd / "x.diff")]}}))
    rc, fr = ex.dispatch_node(_fields("hollow"), root=str(REPO), run_dir=str(rd),
                              plan_path=str(plan), task_class="framework_edit", db=db,
                              run_id="r1", dispatch_fn=lambda *a: (0, ""))
    assert (rc, fr) == (1, "error")
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][0] == "vacuous"
