"""`mini-ork recover` must dispatch the closure it prints, then free the run.

The subcommand runs ``python -m mini_ork.recovery.planner`` in its own process,
so ``main``'s env hand-off never reached an executor: the CLI printed
"dispatching closure" and exited without running a node, holding the lease.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.recovery import planner as rp  # noqa: E402
from test_recover_lease_wiring import SCHEMA_SQL, _seed_success, _workflow  # noqa: E402


def _setup(tmp_path: Path, monkeypatch, *, recipe_env: bool):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    run_id, recipe = "run-cli-1", "framework-edit"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": recipe}))
    workflow = tmp_path / "wf.yaml"
    _workflow(workflow)
    for node in ("A", "B", "C"):  # D failed → closure = {D}
        _seed_success(str(db), run_id, node, recipe, "framework_edit", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_TASK_CLASS", "framework_edit")
    if recipe_env:
        monkeypatch.setenv("MINI_ORK_RECIPE", recipe)
    else:
        monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)
    for key in ("MINI_ORK_LEASE_TOKEN", "MINI_ORK_RECOVERY_REQUEST", "MINI_ORK_WORKFLOW"):
        monkeypatch.delenv(key, raising=False)
    return run_id, str(db), str(workflow)


def test_cli_dispatches_closure_then_releases_lease(tmp_path, monkeypatch):
    run_id, db, workflow = _setup(tmp_path, monkeypatch, recipe_env=True)
    calls = []

    def fake_execute(argv):
        calls.append((argv, os.environ.get("MINI_ORK_RECOVERY_CLOSURE"), os.environ.get("MINI_ORK_RUN_ID")))
        return 0

    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db], execute_fn=fake_execute)

    assert rc == 0
    assert calls == [(["--recovery"], "D", run_id)]
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM run_leases WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    assert con.execute("SELECT status FROM recovery_requests WHERE run_id=?", (run_id,)).fetchone()[0] == "completed"


def test_cli_propagates_executor_failure_and_still_releases(tmp_path, monkeypatch):
    run_id, db, workflow = _setup(tmp_path, monkeypatch, recipe_env=True)
    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db], execute_fn=lambda argv: 3)
    assert rc == 3
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM run_leases WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    assert con.execute("SELECT status FROM recovery_requests WHERE run_id=?", (run_id,)).fetchone()[0] == "failed"


def test_status_never_dispatches(tmp_path, monkeypatch):
    run_id, db, workflow = _setup(tmp_path, monkeypatch, recipe_env=True)
    calls = []
    rc = rp.cli_main([run_id, "--status", "--workflow", workflow, "--db", db], execute_fn=calls.append)
    assert rc == 0 and calls == []


def test_recipe_falls_back_to_run_profile(tmp_path, monkeypatch, capsys):
    run_id, db, workflow = _setup(tmp_path, monkeypatch, recipe_env=False)
    rc = rp.main([run_id, "--status", "--workflow", workflow, "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "recipe:     framework-edit" in out
    assert "[reuse]  A" in out  # checkpoints hash-match once the recipe is known
