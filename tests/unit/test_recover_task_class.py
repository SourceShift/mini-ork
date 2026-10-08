"""A recovered run keeps its task class (kickoff recover-task-class).

``mini-ork recover`` dispatched its closure without publishing the run's task
class, so the recovered ``execute`` resolved ``"generic"`` (this run's
``plan.json`` has no ``task_class``) and the publisher evaluated the wrong gate
set — a code-fix run failed a safety gate. These cover both halves of the fix:

  * ``planner.cli_main`` hands ``MINI_ORK_TASK_CLASS`` to the executor it spawns
    (the ledger row wins; an explicit value is never overridden);
  * ``execute._resolve_task_class`` resolves the class for any entry point
    (plan.json → env → run_profile.json → task_runs row → ``"generic"``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.recovery import planner as rp  # noqa: E402
from test_recover_lease_wiring import SCHEMA_SQL, _seed_success, _workflow  # noqa: E402


def _setup(tmp_path: Path, monkeypatch, *, run_profile_task_class=None,
           run_task_class: str = "code_fix"):
    """A recoverable run: DB ledger row + a plan.json with NO task_class."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    # The E2 harness schema predates the column; add it and seed the row the
    # live bug had (task_runs.task_class = 'code_fix').
    con.execute("ALTER TABLE task_runs ADD COLUMN task_class TEXT")
    con.execute("INSERT INTO task_runs (id, status, task_class) VALUES (?, 'executing', ?)",
                ("run-tc-1", run_task_class))
    con.commit()
    con.close()

    run_id, recipe = "run-tc-1", "code-fix"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    profile = {"recipe": recipe}
    if run_profile_task_class is not None:
        profile["task_class"] = run_profile_task_class
    (run_dir / "run_profile.json").write_text(json.dumps(profile))
    # plan.json carries no task_class — exactly the live bug.
    (run_dir / "plan.json").write_text(json.dumps({"objective": "recover"}))
    workflow = tmp_path / "wf.yaml"
    _workflow(workflow)
    for node in ("A", "B", "C"):  # D failed → closure = {D}, so dispatch happens
        _seed_success(str(db), run_id, node, recipe, run_task_class, str(run_dir))

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RECIPE", recipe)
    monkeypatch.delenv("MINI_ORK_TASK_CLASS", raising=False)
    for key in ("MINI_ORK_LEASE_TOKEN", "MINI_ORK_RECOVERY_REQUEST", "MINI_ORK_WORKFLOW"):
        monkeypatch.delenv(key, raising=False)
    return run_id, str(db), str(workflow)


def test_cli_main_publishes_task_class_from_ledger_row(tmp_path, monkeypatch):
    # run_profile carries a DIFFERENT class so a pass proves the ledger row wins.
    run_id, db, workflow = _setup(tmp_path, monkeypatch,
                                  run_profile_task_class="framework_edit")
    seen: dict[str, str | None] = {}

    def fake_execute(argv):
        seen["task_class"] = os.environ.get("MINI_ORK_TASK_CLASS")
        return 0

    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db], execute_fn=fake_execute)

    assert rc == 0
    assert seen["task_class"] == "code_fix"


def test_cli_main_never_overrides_explicit_task_class(tmp_path, monkeypatch):
    run_id, db, workflow = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MINI_ORK_TASK_CLASS", "other")
    seen: dict[str, str | None] = {}

    def fake_execute(argv):
        seen["task_class"] = os.environ.get("MINI_ORK_TASK_CLASS")
        return 0

    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db], execute_fn=fake_execute)

    assert rc == 0
    assert seen["task_class"] == "other"


def test_resolve_task_class_reads_run_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_TASK_CLASS", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "plan.json").write_text(json.dumps({"objective": "no class here"}))
    (run_dir / "run_profile.json").write_text(json.dumps({"task_class": "code_fix"}))

    assert ex._resolve_task_class(str(run_dir / "plan.json"), "", "") == "code_fix"


def test_resolve_task_class_defaults_to_generic(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_TASK_CLASS", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "plan.json").write_text(json.dumps({"objective": "no class anywhere"}))

    assert ex._resolve_task_class(str(run_dir / "plan.json"), "", "") == "generic"


def test_resolve_task_class_plan_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_TASK_CLASS", "from_env")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"task_class": "code_fix"}))

    assert ex._resolve_task_class(str(plan), "", "") == "code_fix"


def test_resolve_task_class_falls_back_to_ledger_row(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_TASK_CLASS", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({}))  # no class; no run_profile.json beside it
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_class TEXT)")
    con.execute("INSERT INTO task_runs VALUES ('r1', 'code_fix')")
    con.commit()
    con.close()

    assert ex._resolve_task_class(str(plan), "r1", str(db)) == "code_fix"
