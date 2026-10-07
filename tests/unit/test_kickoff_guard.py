"""Kickoff guard: a run must not rewrite its own contract (MO_KICKOFF_GUARD)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork import context_v2
from mini_ork.cli import publisher
from mini_ork.verify import kickoff_guard as kg

KICKOFF = """# Add the thing

## Files in scope (touch ONLY these)

- `mini_ork/kickoff_lint.py`

Do NOT modify any other file.
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("MO_KICKOFF_GUARD", "MINI_ORK_DB", "MINI_ORK_HOME", "MO_CONTEXT_V2_MIN_SEVERITY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def run(tmp_path):
    kickoff = tmp_path / "worktree" / "kickoffs" / "auto" / "task.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text(KICKOFF, encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pack = context_v2.build(str(kickoff), db=str(tmp_path / "none.db"), run_id="r")
    context_v2.write_json(str(run_dir / context_v2.PACK_FILENAME), pack)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, notes TEXT)")
    con.execute("INSERT INTO task_runs (id) VALUES ('r')")
    con.commit()
    con.close()
    return kickoff, run_dir, str(db)


def _widen(kickoff: Path) -> None:
    kickoff.write_text(KICKOFF.replace("- `mini_ork/kickoff_lint.py`",
                                       "- `mini_ork/kickoff_lint.py`\n- `tests/unit/test_extra.py`"),
                       encoding="utf-8")


def _notes(db: str) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COALESCE(notes, '') FROM task_runs WHERE id='r'").fetchone()[0]
    finally:
        con.close()


# ── snapshot + evaluate ─────────────────────────────────────────────────────

def test_the_pack_snapshots_the_contract_before_any_node_runs(run):
    kickoff, run_dir, _db = run
    pack = context_v2.load_pack(str(run_dir))
    assert pack["kickoff_snapshot"] == KICKOFF
    assert len(pack["kickoff_sha256"]) == 64


def test_evaluate_reports_intact_modified_deleted_and_no_snapshot(run, tmp_path):
    kickoff, run_dir, _db = run
    assert kg.evaluate(str(run_dir))["status"] == "intact"
    _widen(kickoff)
    report = kg.evaluate(str(run_dir))
    assert report["status"] == "modified"
    assert any(line == "+- `tests/unit/test_extra.py`" for line in report["diff"])
    kickoff.unlink()
    assert kg.evaluate(str(run_dir))["status"] == "deleted"
    empty = tmp_path / "empty-run"
    empty.mkdir()
    assert kg.evaluate(str(empty))["status"] == "no_snapshot"
    context_v2.write_json(str(empty / context_v2.PACK_FILENAME), {"kickoff_path": str(kickoff)})
    assert kg.evaluate(str(empty))["status"] == "no_snapshot"  # a pack from before the snapshot


def test_shadow_reports_but_never_restores(run):
    kickoff, run_dir, _db = run
    _widen(kickoff)
    ok, reason, report = kg.publish_gate(str(run_dir), gate_mode="shadow")
    assert not ok and reason == "kickoff_modified_by_run" and report["would_block"]
    assert "test_extra.py" in kickoff.read_text()
    assert json.loads((run_dir / kg.REPORT).read_text())["status"] == "modified"


def test_on_restores_the_contract(run):
    kickoff, run_dir, _db = run
    _widen(kickoff)
    ok, _reason, report = kg.publish_gate(str(run_dir), gate_mode="on")
    assert not ok and report["restored"] is True
    assert kickoff.read_text(encoding="utf-8") == KICKOFF


def test_mode_defaults_to_shadow(monkeypatch):
    assert kg.mode() == "shadow"
    monkeypatch.setenv("MO_KICKOFF_GUARD", "ON")
    assert kg.mode() == "on"
    monkeypatch.setenv("MO_KICKOFF_GUARD", "sometimes")
    assert kg.mode() == "shadow"


# ── the publisher hook ──────────────────────────────────────────────────────

def test_publisher_shadow_notes_but_lets_the_publish_continue(run):
    kickoff, run_dir, db = run
    _widen(kickoff)
    assert publisher._kickoff_guard(str(run_dir), db, "r") is None
    assert "[shadow] kickoff guard would block: kickoff_modified_by_run" in _notes(db)
    assert "test_extra.py" in kickoff.read_text()


def test_publisher_on_blocks_restores_and_notes(run, monkeypatch, capsys):
    kickoff, run_dir, db = run
    monkeypatch.setenv("MO_KICKOFF_GUARD", "on")
    kickoff.unlink()
    assert publisher._kickoff_guard(str(run_dir), db, "r") == (1, "verdict_fail")
    assert "kickoff_guard: kickoff_deleted_by_run" in _notes(db)
    assert kickoff.read_text(encoding="utf-8") == KICKOFF
    assert "[BLOCK] kickoff-guard" in capsys.readouterr().out


def test_publisher_passes_an_intact_contract_silently(run, monkeypatch):
    _kickoff, run_dir, db = run
    monkeypatch.setenv("MO_KICKOFF_GUARD", "on")
    assert publisher._kickoff_guard(str(run_dir), db, "r") is None
    assert _notes(db) == ""


def test_publisher_off_does_nothing(run, monkeypatch):
    kickoff, run_dir, db = run
    monkeypatch.setenv("MO_KICKOFF_GUARD", "off")
    _widen(kickoff)
    assert publisher._kickoff_guard(str(run_dir), db, "r") is None
    assert not (run_dir / kg.REPORT).exists() and _notes(db) == ""


def test_publisher_node_runs_the_guard_before_delivering():
    import inspect
    src = inspect.getsource(publisher.publisher_node)
    assert "_kickoff_guard(run_dir, db, run_id)" in src
    # after the probe-validity gate, before the artifact contract is delivered
    assert src.index("MO_PROBE_VALIDITY") < src.index("_kickoff_guard(") < src.index("# ── artifact contract")
