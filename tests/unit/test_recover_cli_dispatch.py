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


def _setup(tmp_path: Path, monkeypatch, *, recipe_env: bool,
           roots_target: str | None = None):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    run_id, recipe = "run-cli-1", "framework-edit"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    profile = {"recipe": recipe}
    if roots_target is not None:
        # The persisted roots record the normal dispatcher writes; only this is
        # authoritative enough to hand a recovered closure its target.
        profile["roots"] = {"target": roots_target, "run_dir": str(run_dir),
                            "home": str(tmp_path), "engine": str(tmp_path),
                            "exec_cwd": None}
    (run_dir / "run_profile.json").write_text(json.dumps(profile))
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
    for key in ("MINI_ORK_LEASE_TOKEN", "MINI_ORK_RECOVERY_REQUEST", "MINI_ORK_WORKFLOW",
                "MO_TARGET_CWD"):
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


def test_cli_clears_a_stale_stop_request_before_dispatching(tmp_path, monkeypatch):
    """`POST /stop` writes `.stop-requested` and only the hard-kill path unlinks
    it, so a stopped run carries the marker into `recover` — where execute honours
    it before the first node, skipping the whole closure and failing the run again
    while the command still exits 0."""
    run_id, db, workflow = _setup(tmp_path, monkeypatch, recipe_env=True)
    run_dir = Path(os.environ["MINI_ORK_RUN_DIR"])
    (run_dir / ".stop-requested").write_text("1792142272\n")
    calls = []

    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db],
                     execute_fn=lambda argv: calls.append(argv) or 0)

    assert rc == 0
    assert calls == [["--recovery"]]
    assert not (run_dir / ".stop-requested").exists()


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


def _target_seen_by_the_executor(run_id, db, workflow) -> str | None:
    seen: dict = {}
    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db],
                     execute_fn=lambda argv: seen.update(
                         argv=argv, target=os.environ.get("MO_TARGET_CWD")) or 0)
    assert rc == 0
    return seen["target"]


def test_closure_without_the_implementer_still_grades_the_runs_target(
        tmp_path, monkeypatch):
    """Only the implementer handler ever publishes ``MO_TARGET_CWD``, and a
    verifier-rooted closure has no implementer in it — so every verifier resolves
    the repo under test from ``MINI_ORK_ROOT`` else ``os.getcwd()``, i.e. the
    directory the operator ran the recover from. framework-edit's static-check
    then ran ``git -C <that directory> apply --check`` and failed
    ``diff-apply-check-clean`` on a perfectly good diff (rp-e3-header-20261015-222115
    and rp-e6-graph-focus, 2026-10-16). The run's own record decides."""
    target = tmp_path / "worktree"
    target.mkdir()
    run_id, db, workflow = _setup(tmp_path, monkeypatch, recipe_env=True,
                                  roots_target=str(target))
    assert _target_seen_by_the_executor(run_id, db, workflow) == str(target)


def test_an_operator_target_is_never_overridden(tmp_path, monkeypatch):
    """``MO_TARGET_CWD`` is the documented lever for re-pointing a run whose
    worktree was cleaned, so an explicit value must survive the recover."""
    recorded, operator = tmp_path / "worktree", tmp_path / "elsewhere"
    recorded.mkdir()
    operator.mkdir()
    run_id, db, workflow = _setup(tmp_path, monkeypatch, recipe_env=True,
                                  roots_target=str(recorded))
    monkeypatch.setenv("MO_TARGET_CWD", str(operator))
    assert _target_seen_by_the_executor(run_id, db, workflow) == str(operator)


def test_a_recorded_target_that_is_gone_publishes_nothing(tmp_path, monkeypatch):
    """A missing worktree is named by ``require_run_target`` at first use; a
    recover must not stand in for it with the operator's cwd."""
    run_id, db, workflow = _setup(tmp_path, monkeypatch, recipe_env=True,
                                  roots_target=str(tmp_path / "gone"))
    assert _target_seen_by_the_executor(run_id, db, workflow) is None
