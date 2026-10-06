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


def test_card_files_map_onto_the_project_when_the_worktree_is_gone(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "guide.md").write_text("x\n")
    gone = str(tmp_path / "worktrees" / "proj" / "task-abc123" / "docs" / "guide.md")
    fields = board_cmd._card_fields(
        {"files": [{"path": gone, "added": 3, "removed": 1},
                   {"path": str(tmp_path / "nowhere" / "x.py"), "added": 1, "removed": 0}],
         "steps": [{"node_id": "editor", "node_type": "implementer", "lane": "worker",
                    "duration": 44, "state": "done"}],
         "cost_by_stage": {"worker": 0.37, "learning": 0.0}, "cost_total": 0.37,
         "verdict": {"verdict": "pass"}},
        project)
    assert fields["files"][0] == {"path": "docs/guide.md", "abs": str(project / "docs" / "guide.md"),
                                  "added": 3, "removed": 1}
    assert fields["files"][1]["abs"] is None
    assert fields["steps"] == [{"name": "editor", "type": "implementer", "lane": "worker",
                                "seconds": 44, "state": "done"}]
    assert fields["cost_by_stage"] == {"worker": 0.37}
    assert fields["verdict"] == "pass"


def test_artifacts_are_grouped_for_the_run_tab(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-x"
    (run_dir / "evidence").mkdir(parents=True)
    for rel in ("kickoff.md", "plan.json", "verdict.json", "framework-edit.diff",
                "agent-editor.live.jsonl", "execute.log", "evidence/test-1.log", "notes.txt"):
        (run_dir / rel).write_text("x")
    groups = [(a["group"], a["path"]) for a in board_cmd._artifacts(run_dir)]
    assert groups == [
        ("Kickoff & plan", "kickoff.md"), ("Kickoff & plan", "plan.json"),
        ("Results", "verdict.json"), ("Diffs", "framework-edit.diff"),
        ("Agent output", "agent-editor.live.jsonl"), ("Logs", "execute.log"),
        ("Evidence", "evidence/test-1.log"), ("Other", "notes.txt"),
    ]
    assert board_cmd._artifacts(tmp_path / "missing") == []
