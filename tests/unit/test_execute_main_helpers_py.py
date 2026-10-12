"""Unit tests for the execute main() phase helpers (SOLID A5)."""
import os

import mini_ork.cli.execute as ex


def test_parse_defaults_from_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", "/p/plan.json")
    args, rc = ex._parse_execute_argv([])
    assert rc == 0 and args.dry_run is True and args.plan_path == "/p/plan.json"


def test_parse_flags_and_positional():
    args, rc = ex._parse_execute_argv(
        ["p.json", "--node-type", "implementer", "--dispatch-mode", "parallel"])
    assert rc == 0
    assert args.plan_path == "p.json"
    assert args.filter_node_type == "implementer"
    assert args.dispatch_mode_override == "parallel"


def test_parse_unknown_flag_and_extra_positional(capsys):
    assert ex._parse_execute_argv(["--bogus"])[1] == 2
    assert "Unknown flag: --bogus" in capsys.readouterr().err
    assert ex._parse_execute_argv(["a.json", "b.json"])[1] == 2
    assert "Unexpected argument: b.json" in capsys.readouterr().err


def test_parse_help(capsys):
    args, rc = ex._parse_execute_argv(["--help"])
    assert args is None and rc == 0
    assert "Usage: mini-ork execute" in capsys.readouterr().out


def test_resolve_plan_missing_errors(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_WORKFLOW", raising=False)
    monkeypatch.delenv("MINI_ORK_RECOVERY_CLOSURE", raising=False)
    monkeypatch.delenv("MINI_ORK_RECOVERY_FROM", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    path, rc = ex._resolve_plan_path("", str(tmp_path), from_node="", recovery_active=False)
    assert rc == 2 and path == ""
    assert "No plan.json found" in capsys.readouterr().err


def test_resolve_plan_newest_wins(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    older = runs / "r1"
    newer = runs / "r2"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "plan.json").write_text("{}")
    (newer / "plan.json").write_text("{}")
    os.utime(older / "plan.json", (1000, 1000))
    os.utime(newer / "plan.json", (2000, 2000))
    monkeypatch.delenv("MINI_ORK_WORKFLOW", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    path, rc = ex._resolve_plan_path("", str(tmp_path), from_node="", recovery_active=False)
    assert rc == 0 and path == str(newer / "plan.json")


def test_resolve_plan_prefers_the_runs_own_plan_over_a_strangers(tmp_path, monkeypatch):
    """A run whose plan is not named ``plan.json`` must still resolve its OWN.

    epic-runner's planner writes ``epic-runner-plan.json``, so the home-wide
    newest-``plan.json`` walk adopted whichever *other* run finished last --
    typically the epic child's framework-edit plan. Its artifact contract then
    expanded against the parent's ``MINI_ORK_RUN_DIR``, so ``epic_verifier``
    required a ``framework-edit.diff`` the parent never writes and the run
    false-failed the hollow-run guard despite a passing rubric
    (run-1791785967-43108, 2026-10-12).
    """
    runs = tmp_path / "runs"
    parent = runs / "run-parent"
    child = runs / "run-parent-epic"
    parent.mkdir(parents=True)
    child.mkdir(parents=True)
    (parent / "epic-runner-plan.json").write_text("{}")
    # The child's plan.json is NEWER, so the old walk would have chosen it.
    (child / "plan.json").write_text("{}")
    os.utime(parent / "epic-runner-plan.json", (1000, 1000))
    os.utime(child / "plan.json", (2000, 2000))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(parent))
    monkeypatch.delenv("MINI_ORK_WORKFLOW", raising=False)
    path, rc = ex._resolve_plan_path("", str(tmp_path), from_node="", recovery_active=False)
    assert rc == 0 and path == str(parent / "epic-runner-plan.json")


def test_resolve_plan_own_plan_json_beats_a_newer_stranger(tmp_path, monkeypatch):
    """Even a plain ``plan.json`` is resolved from THIS run when one exists."""
    runs = tmp_path / "runs"
    mine = runs / "run-mine"
    theirs = runs / "run-theirs"
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)
    (mine / "plan.json").write_text("{}")
    (theirs / "plan.json").write_text("{}")
    os.utime(mine / "plan.json", (1000, 1000))
    os.utime(theirs / "plan.json", (2000, 2000))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(mine))
    monkeypatch.delenv("MINI_ORK_WORKFLOW", raising=False)
    path, rc = ex._resolve_plan_path("", str(tmp_path), from_node="", recovery_active=False)
    assert rc == 0 and path == str(mine / "plan.json")


def test_recovery_filter_requires_context(capsys, monkeypatch):
    monkeypatch.delenv("MINI_ORK_RECOVERY_CLOSURE", raising=False)
    monkeypatch.delenv("MINI_ORK_RECOVERY_FROM", raising=False)
    _, rc = ex._apply_recovery_filter([], from_node="", recovery_active=True,
                                      repair_budget="", workflow="")
    assert rc == 2
    assert "--recovery requires" in capsys.readouterr().err


def test_recovery_filter_narrows_to_closure(monkeypatch):
    monkeypatch.setenv("MINI_ORK_RECOVERY_CLOSURE", "impl review")
    monkeypatch.delenv("MINI_ORK_RECOVERY_FROM", raising=False)
    node_ids = [f"plan{ex._SEP}x", f"impl{ex._SEP}y", f"review{ex._SEP}z"]
    filtered, rc = ex._apply_recovery_filter(
        node_ids, from_node="", recovery_active=False, repair_budget="", workflow="")
    assert rc == 0
    assert [e.split(ex._SEP, 1)[0] for e in filtered] == ["impl", "review"]
    assert os.environ["MINI_ORK_RECOVERY_ACTIVE"] == "1"


def test_recovery_filter_bad_budget(capsys, monkeypatch):
    monkeypatch.setenv("MINI_ORK_RECOVERY_CLOSURE", "impl")
    _, rc = ex._apply_recovery_filter([], from_node="", recovery_active=False,
                                      repair_budget="abc", workflow="")
    assert rc == 2
    assert "must be a positive number" in capsys.readouterr().err


def _plan_db(tmp_path):
    import sqlite3

    db = str(tmp_path / "state.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, plan_path TEXT, updated_at INTEGER)")
    con.execute("INSERT INTO task_runs VALUES ('run-1', '', 0)")
    con.commit()
    con.close()
    return db


def test_record_plan_artifact_stamps_an_empty_plan_path(tmp_path):
    """A recipe that plans inside its DAG (epic-runner) left ``plan_path`` null,
    so every reader keyed on the column reported "no plan" for a run whose plan
    was on disk the whole time."""
    import sqlite3

    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "epic-runner-plan.json").write_text('{"objective": "o"}')
    db = _plan_db(tmp_path)

    ex._record_plan_artifact(db, "run-1", str(run_dir))

    con = sqlite3.connect(db)
    row = con.execute("SELECT plan_path FROM task_runs WHERE id='run-1'").fetchone()
    con.close()
    assert row[0] == str(run_dir / "epic-runner-plan.json")


def test_record_plan_artifact_never_overwrites_a_recorded_plan(tmp_path):
    """``mini-ork plan`` recorded the plan the DAG was built from; a later scan
    by name must not replace it."""
    import sqlite3

    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text("{}")
    db = _plan_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE task_runs SET plan_path='/elsewhere/plan.json' WHERE id='run-1'")
    con.commit()
    con.close()

    ex._record_plan_artifact(db, "run-1", str(run_dir))

    con = sqlite3.connect(db)
    row = con.execute("SELECT plan_path FROM task_runs WHERE id='run-1'").fetchone()
    con.close()
    assert row[0] == "/elsewhere/plan.json"


def test_record_plan_artifact_is_quiet_when_no_plan_exists(tmp_path):
    import sqlite3

    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    db = _plan_db(tmp_path)

    ex._record_plan_artifact(db, "run-1", str(run_dir))

    con = sqlite3.connect(db)
    row = con.execute("SELECT plan_path FROM task_runs WHERE id='run-1'").fetchone()
    con.close()
    assert row[0] == ""
