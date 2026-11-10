"""A resumed run must not grade the verdict of the attempt it replaced.

``mini-ork recover`` re-runs a closure over the run dir the failed attempt
left behind. That dir still holds ``verdict.json`` (and sometimes
``run-verdict.json``), and the rubric grader reads whichever file is at the
root — so a resumed run could be judged on the attempt that already failed.

The fix archives them under ``attempts/<n>/`` before the recovery dispatches,
using the same attempt number the publisher archives its markers under, so one
directory holds one attempt's whole record.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.recovery import planner as rp  # noqa: E402
from test_recover_lease_wiring import SCHEMA_SQL  # noqa: E402
from test_recover_task_class import _setup  # noqa: E402


def _run_dir(tmp_path: Path, notes: str = "attempt 1 started 2026-11-10T10:00:00Z",
             verdicts=("verdict.json",)) -> Path:
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    (run / "run_profile.json").write_text(json.dumps({"recipe": "code-fix"}))
    (run / "plan.json").write_text(json.dumps({"objective": "recover"}))
    for name in verdicts:
        (run / name).write_text(json.dumps({"verdict": "fail"}))
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    # The shared harness schema predates the live-status ``notes`` column.
    con.execute("ALTER TABLE task_runs ADD COLUMN notes TEXT")
    con.execute("INSERT INTO task_runs (id, status, notes) VALUES (?, 'failed', ?)",
                ("run-v1", notes))
    con.commit()
    con.close()
    return run


# ── the helper ──────────────────────────────────────────────────────────────


def test_the_previous_verdict_moves_into_its_attempt_dir(tmp_path):
    run = _run_dir(tmp_path, verdicts=("verdict.json", "run-verdict.json"))

    target = rp._archive_superseded_verdicts(str(run), str(tmp_path / "state.db"), "run-v1")

    assert target == str(run / "attempts" / "1")
    assert sorted(os.listdir(target)) == ["run-verdict.json", "verdict.json"]
    assert not (run / "verdict.json").exists()
    assert not (run / "run-verdict.json").exists()


def test_the_attempt_number_comes_from_the_ledger(tmp_path):
    run = _run_dir(tmp_path, notes=(
        "attempt 1 started 2026-11-10T09:00:00Z; "
        "attempt 2 started 2026-11-10T10:00:00Z"))

    target = rp._archive_superseded_verdicts(str(run), str(tmp_path / "state.db"), "run-v1")

    assert target == str(run / "attempts" / "2")


def test_a_run_with_no_verdict_is_left_alone(tmp_path):
    run = _run_dir(tmp_path, verdicts=())

    assert rp._archive_superseded_verdicts(str(run), str(tmp_path / "state.db"), "run-v1") == ""
    assert not (run / "attempts").exists()


def test_an_unreadable_ledger_still_archives_under_attempt_one(tmp_path):
    """The archive must not be what fails a recovery."""
    run = _run_dir(tmp_path)

    target = rp._archive_superseded_verdicts(str(run), str(tmp_path / "gone.db"), "run-v1")

    assert target == str(run / "attempts" / "1")
    assert (Path(target) / "verdict.json").is_file()


# ── the recovery path ───────────────────────────────────────────────────────


def test_cli_main_archives_before_it_dispatches(tmp_path, monkeypatch, capsys):
    run_id, db, workflow = _setup(tmp_path, monkeypatch)
    run = tmp_path / "run"
    (run / "verdict.json").write_text(json.dumps({"verdict": "fail"}))
    seen: dict[str, bool] = {}

    def fake_execute(argv):
        # The executor must not be able to see the replaced attempt's verdict.
        seen["root_verdict_present"] = (run / "verdict.json").exists()
        seen["archived"] = (run / "attempts" / "1" / "verdict.json").is_file()
        return 0

    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db], execute_fn=fake_execute)

    assert rc == 0
    assert seen == {"root_verdict_present": False, "archived": True}
    assert "archived the superseded verdict" in capsys.readouterr().out
