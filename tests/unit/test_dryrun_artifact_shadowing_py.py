"""A rehearsal sharing a run dir must not occupy the live run's own record.

Regression origin: `.mini-ork/runs/child-1790341005-57997`. A live code-fix
lifecycle (8 nodes, 3 failed, reviewer `needs_revision`) shared its run dir
with a DRY_RUN lifecycle of a *different* recipe (docs, 5 nodes) that had
inherited `MINI_ORK_RUN_ID`. Two first-writer-wins guards let the rehearsal
win: `execute.log` was written only `if not isfile`, and `verdict.json` — once
it carried `source == "execute@run-level"` from the rehearsal — made the live
emit return early. The run dir then recorded a rehearsal of the docs recipe as
if it were the live run's outcome.
"""

import json
import os

from mini_ork.cli import execute
from mini_ork.cli import main as cli_main


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_dry_run_verdict_lands_beside_not_on_verdict_json(tmp_path, capsys):
    execute._emit_run_verdict(str(tmp_path), 0, 5, dry_run=True)
    capsys.readouterr()

    assert (tmp_path / "verdict.dryrun.json").is_file()
    assert not (tmp_path / "verdict.json").exists()
    assert not (tmp_path / "run-verdict.json").exists()
    assert _read(tmp_path / "verdict.dryrun.json")["dispatched"] == 5


def test_live_verdict_survives_a_prior_rehearsal(tmp_path, capsys):
    """The exact child-1790341005-57997 corruption: rehearsal first, live second."""
    execute._emit_run_verdict(str(tmp_path), 0, 5, dry_run=True)
    execute._emit_run_verdict(str(tmp_path), 3, 8)
    capsys.readouterr()

    live = _read(tmp_path / "verdict.json")
    assert live["dispatched"] == 8
    assert live["failed_nodes"] == 3
    assert live["verdict"] == "fail"
    assert _read(tmp_path / "verdict.dryrun.json")["dispatched"] == 5


def test_live_verdict_is_idempotent(tmp_path, capsys):
    execute._emit_run_verdict(str(tmp_path), 0, 8)
    first = (tmp_path / "verdict.json").read_text(encoding="utf-8")
    execute._emit_run_verdict(str(tmp_path), 3, 99)
    capsys.readouterr()

    assert (tmp_path / "verdict.json").read_text(encoding="utf-8") == first


def test_live_verdict_still_defers_to_a_recipe_owned_verdict(tmp_path, capsys):
    (tmp_path / "verdict.json").write_text(
        json.dumps({"verdict": "pass", "detail": "recipe-owned"}), encoding="utf-8")
    execute._emit_run_verdict(str(tmp_path), 0, 8)
    capsys.readouterr()

    assert _read(tmp_path / "verdict.json")["detail"] == "recipe-owned"
    assert _read(tmp_path / "run-verdict.json")["source"] == "execute@run-level"


def test_execute_log_name_follows_the_dry_run_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    assert cli_main._execute_log_name() == "execute.dryrun.log"
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "0")
    assert cli_main._execute_log_name() == "execute.log"
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)
    assert cli_main._execute_log_name() == "execute.log"


def test_rehearsal_and_live_logs_are_distinct_files(tmp_path, monkeypatch):
    """The two lifecycles that shared the child run dir must not share a log."""
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    rehearsal = os.path.join(str(tmp_path), cli_main._execute_log_name())
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "0")
    live = os.path.join(str(tmp_path), cli_main._execute_log_name())

    assert rehearsal != live
    assert rehearsal.endswith("execute.dryrun.log")
    assert live.endswith("execute.log")
