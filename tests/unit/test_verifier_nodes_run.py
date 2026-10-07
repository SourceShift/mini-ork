"""K0.5c — verifier nodes: real durations, and a node that never ran says so.

AC0: the fallback node_end (handlers that never call trace(): verifier,
publisher, rollback, eval) records the real duration. It was a hard-coded 0,
so every Python-era verifier node_end read "0 ms" whether it ran or not.
AC2: a verifier node that fails before running its script fails with reason
``verifier_not_executed`` (node_end payload verdict, a [fail] line, task_runs
notes) — never a bare ``error``. It leaves no verifier evidence, so I1 counts
it as not proven.
AC1 (root cause of the 2026-09/10 verifier errors) is in the commit message:
the pre-run hollow-run guard required the verifier-written verdict.json, fixed
upstream in ad37d26f (85 verifier errors / 56 runs before it, 1 after).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
import sys  # noqa: E402

sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.verify import probe_validity as pv  # noqa: E402


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


def _node_end(db: str, node_id: str) -> tuple[str | None, dict]:
    con = sqlite3.connect(db)
    row = con.execute("SELECT finish_reason, payload_json FROM run_events WHERE run_id='r1' AND event_type='node_end' "
                      "AND json_extract(payload_json,'$.node_id')=?", (node_id,)).fetchone()
    con.close()
    return row[0], json.loads(row[1])


def _notes(db: str) -> str:
    con = sqlite3.connect(db)
    out = con.execute("SELECT coalesce(notes,'') FROM task_runs WHERE id='r1'").fetchone()[0]
    con.close()
    return out


def _self_migrate(tmp: Path, db: str, monkeypatch, node_id: str, vref: str, run_verifier=None):
    """A pre-implementation verifier of the self-migrate recipe (its guard is skipped)."""
    rd = tmp / "run"
    rd.mkdir(exist_ok=True)
    plan = tmp / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "artifact_contract": {"outputs": [str(rd / "self-migrate.diff")]}}))
    if run_verifier is not None:
        monkeypatch.setattr(ex, "_run_verifier_ref", run_verifier)
    return ex.dispatch_node(_fields(node_id, vref), root=str(REPO), run_dir=str(rd), plan_path=str(plan),
                            task_class="self_migrate", db=db, run_id="r1", dispatch_fn=lambda *a: (0, ""),
                            recipe="self-migrate", workflow=str(REPO / "recipes" / "self-migrate" / "workflow.yaml"))


def test_fallback_node_end_records_the_real_duration(tmp_path, monkeypatch) -> None:
    db = _seed(tmp_path)

    def slow_passing_verifier(script, evidence_path, **_kw):
        time.sleep(0.25)
        Path(evidence_path).write_text('{"pass":true}\n')
        return 0

    rc, fr = _self_migrate(tmp_path, db, monkeypatch, "pre_retirement_parity",
                           "verifiers/pre-retirement-parity.py", slow_passing_verifier)
    assert (rc, fr) == (0, "done")
    finish, payload = _node_end(db, "pre_retirement_parity")
    assert finish == "done" and payload["duration_ms"] >= 200
    assert "verdict" not in payload  # no note → payload unchanged apart from the duration


def test_missing_verifier_ref_is_verifier_not_executed(tmp_path, monkeypatch, capsys) -> None:
    # A pre-implementation node (artifact guard skipped) whose script does not exist.
    db = _seed(tmp_path)
    rc, fr = _self_migrate(tmp_path, db, monkeypatch, "pre_retirement_parity", "verifiers/does-not-exist.py")
    assert (rc, fr) == (1, "error")  # finish_reason stays the CHECK-enum value
    finish, payload = _node_end(db, "pre_retirement_parity")
    assert finish == "error" and payload["verdict"] == "verifier_not_executed"
    assert "verifier_not_executed: node pre_retirement_parity (verifier_ref not found" in _notes(db)
    assert "verifier node pre_retirement_parity: verifier_not_executed" in capsys.readouterr().err


def test_missing_required_artifact_before_running_is_verifier_not_executed(tmp_path) -> None:
    db = _seed(tmp_path)
    rd = tmp_path / "run"
    rd.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "artifact_contract": {"required_artifacts": [str(rd / "x.diff")]}}))
    rc, fr = ex.dispatch_node(_fields("hollow"), root=str(REPO), run_dir=str(rd), plan_path=str(plan),
                              task_class="framework_edit", db=db, run_id="r1", dispatch_fn=lambda *a: (0, ""))
    assert (rc, fr) == (1, "error")
    payload = _node_end(db, "hollow")[1]
    assert payload["verdict"] == "verifier_not_executed"
    assert "required artifact(s) missing or empty before the verifier ran" in _notes(db)
    # I1 consistency: a verifier that never ran proves nothing.
    assert pv.verify_proven(db, "r1", str(rd))[0] is False
