"""``mini-ork board`` — the JSON the IDE panels read."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed_run(home: Path, run_id: str, status: str) -> None:
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, task_class, "
        "kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", status, 0.5, now, now, "code_fix", "", "latest"))
    con.execute(
        "INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, "
        "confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)",
        (f"gr-{run_id}", "verifier.code_fix", "checks unclear", "record the checks", "{}", 0.4, now, "code_fix"))
    con.commit()
    con.close()


def test_board_has_every_section(home: Path) -> None:
    _seed_run(home, "run-1791000000-aaaaaa", "published")
    b = board_cmd.board(home)
    assert b["version"] == 1 and b["project"] == "proj"
    assert [r["id"] for r in b["runs"]] == ["run-1791000000-aaaaaa"]
    assert b["runs"][0]["state"] == "done"
    assert b["learnings"][0]["kind"] == "gradient"
    assert b["learnings"][0]["suggestion"] == "record the checks"
    for key in ("automations", "recipes", "workspaces"):
        assert isinstance(b[key], list)
    assert b["errors"] == {}
    json.dumps(b, default=str)  # serialisable


def test_a_broken_section_does_not_blank_the_board(home: Path, monkeypatch) -> None:
    _seed_run(home, "run-1791000000-bbbbbb", "failed")

    def boom(_home):
        raise RuntimeError("db locked")

    monkeypatch.setattr(board_cmd, "_learnings", boom)
    b = board_cmd.board(home)
    assert b["learnings"] == [] and "db locked" in b["errors"]["learnings"]
    assert len(b["runs"]) == 1


def test_cli_usage_and_actions(home: Path, capsys) -> None:
    assert board_cmd.main(["merge", "--home", str(home)], "") == 2
    assert board_cmd.main(["merge", "run-x", "--home", str(home)], "") == 1
    assert "no open workspace" in json.loads(capsys.readouterr().out)["error"]
    assert board_cmd.main(["--home", str(home), "--json"], "") == 0
    assert "runs" in json.loads(capsys.readouterr().out)
