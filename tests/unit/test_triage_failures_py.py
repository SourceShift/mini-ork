"""Unit tests for mini_ork.triage.failures — the I/O triage driver."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from mini_ork.triage.failures import latest_failed_run, load_failures, triage_run

# G8's ``_exercise_once`` (mini_ork/rsi/cycle.py) runs this file's scoped tests
# against a *copy* of the measured DB and exports that copy as ``MINI_ORK_DB``.
# The autouse fixture in ``tests/conftest.py`` pops ``MINI_ORK_DB`` before every
# test, so the value is visible only at import time. Capture it here.
_G8_DB = os.environ.get("MINI_ORK_DB") or ""


def _is_exercise_copy(db: str) -> bool:
    """True only for G8's ``_exercise_once`` copy of the measured DB.

    Its shape (``mini_ork/rsi/cycle.py``) is
    ``<home>/rsi/<cycle_id>/exercise-{base,post}.db`` beside an
    ``exercise-{base,post}-home`` dir. A node lane also exports ``MINI_ORK_DB``
    pointing at the operator's live ``state.db``; the shape check keeps this
    file from writing there.
    """
    path = Path(db)
    return (path.name.startswith("exercise-") and path.suffix == ".db"
            and path.parent.parent.name == "rsi"
            and (path.parent / f"{path.stem}-home").is_dir())

_RUN_EVENTS_DDL = """
CREATE TABLE run_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  parent_run_id TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  finish_reason TEXT
)
"""

_TASK_RUNS_DDL = """
CREATE TABLE task_runs (
  id TEXT PRIMARY KEY,
  task_class TEXT,
  recipe TEXT,
  status TEXT,
  updated_at INTEGER
)
"""

_BUG_REPORTS_DDL = """
CREATE TABLE bug_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL UNIQUE,
  run_id TEXT,
  agent_role TEXT NOT NULL,
  task_class TEXT,
  observed_in TEXT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  suggested_fix TEXT,
  severity TEXT NOT NULL DEFAULT 'medium',
  confidence REAL NOT NULL DEFAULT 0.5,
  frequency INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'open',
  promoted_to_epic_id TEXT,
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
)
"""

_EPICS_DDL = """
CREATE TABLE epics (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  lane TEXT,
  worker_default TEXT,
  reviewer TEXT DEFAULT 'sonnet',
  group_id TEXT,
  kickoff_path TEXT,
  estimated_days REAL,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at TEXT,
  primary_journey_id TEXT,
  epic_kind TEXT,
  salvage_attempts INTEGER NOT NULL DEFAULT 0,
  last_conflict_kind TEXT,
  last_conflict_at TEXT
)
"""

_FW_TRACE_TMPL = 'File "{root}/mini_ork/cli/execute.py", line 10, in run\nRuntimeError: kablammo\n'

RUN_ID = "run-fix-1"


def _make_env(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    (root / "mini_ork" / "cli").mkdir(parents=True)
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "verifier-cycle_gate.log").write_text(
        _FW_TRACE_TMPL.format(root=root), encoding="utf-8"
    )

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    for ddl in (_RUN_EVENTS_DDL, _TASK_RUNS_DDL, _BUG_REPORTS_DDL, _EPICS_DDL):
        con.execute(ddl)
    con.execute(
        "INSERT INTO task_runs(id, task_class, recipe, status, updated_at) VALUES(?,?,?,?,?)",
        (RUN_ID, "acq_wave", "acq-wave5-rsi", "failed", 1000),
    )
    con.execute(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json, finish_reason) "
        "VALUES(?,?,?,?,?)",
        (
            "ev1", RUN_ID, "node_end",
            json.dumps({"node_id": "cycle_gate", "node_type": "verifier", "duration_ms": 9000}),
            "error",
        ),
    )
    con.commit()
    con.close()

    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    return home, db, root


def test_load_failures_reads_failing_nodes(tmp_path, monkeypatch):
    home, db, _root = _make_env(tmp_path, monkeypatch)
    failures = load_failures(str(db), RUN_ID, home=str(home))
    assert [f.node_id for f in failures] == ["cycle_gate"]
    assert failures[0].finish_reason == "error"
    assert "kablammo" in failures[0].log_excerpt


def test_dry_run_attributes_without_writing(tmp_path, monkeypatch):
    home, db, root = _make_env(tmp_path, monkeypatch)
    res = triage_run(RUN_ID, home=str(home), db=str(db), root=str(root), dry_run=True)
    assert res.blame == "mini_ork"
    assert res.dry_run is True
    assert res.bug_id is None

    con = sqlite3.connect(db)
    count = con.execute("SELECT COUNT(*) FROM bug_reports").fetchone()[0]
    con.close()
    assert count == 0


def test_triage_emits_bug_report_when_mini_ork(tmp_path, monkeypatch):
    home, db, root = _make_env(tmp_path, monkeypatch)
    res = triage_run(RUN_ID, home=str(home), db=str(db), root=str(root))
    assert res.blame == "mini_ork"
    assert res.bug_id is not None

    con = sqlite3.connect(db)
    row = con.execute("SELECT title, severity, status FROM bug_reports WHERE id=?", (res.bug_id,)).fetchone()
    con.close()
    assert row[1] == "high"
    assert row[2] == "open"


def test_triage_dedupes_recurrences(tmp_path, monkeypatch):
    home, db, root = _make_env(tmp_path, monkeypatch)
    res1 = triage_run(RUN_ID, home=str(home), db=str(db), root=str(root))
    res2 = triage_run(RUN_ID, home=str(home), db=str(db), root=str(root))
    assert res1.bug_id == res2.bug_id

    con = sqlite3.connect(db)
    rows = con.execute("SELECT COUNT(*) FROM bug_reports").fetchone()[0]
    con.close()
    assert rows == 1


def test_triage_skips_consumer_blame(tmp_path, monkeypatch):
    home, db, root = _make_env(tmp_path, monkeypatch)
    # Overwrite the log with a non-framework traceback.
    (home / "runs" / RUN_ID / "verifier-cycle_gate.log").write_text(
        'File "/tmp/other/verify.py", line 1, in m\nZeroDivisionError: x\n', encoding="utf-8"
    )
    res = triage_run(RUN_ID, home=str(home), db=str(db), root=str(root))
    assert res.blame == "consumer"
    assert res.bug_id is None


def test_promote_creates_framework_edit_epic(tmp_path, monkeypatch):
    home, db, root = _make_env(tmp_path, monkeypatch)
    res = triage_run(RUN_ID, home=str(home), db=str(db), root=str(root), promote=True)
    assert res.blame == "mini_ork"
    assert res.epic_id is not None

    con = sqlite3.connect(db)
    recipe = con.execute("SELECT recipe FROM epics WHERE id=?", (res.epic_id,)).fetchone()[0]
    status = con.execute("SELECT status FROM bug_reports WHERE id=?", (res.bug_id,)).fetchone()[0]
    con.close()
    assert recipe == "framework-edit"
    assert status == "queued_as_epic"

    kickoff = Path(res.kickoff_path)
    assert kickoff.is_file()
    body = kickoff.read_text(encoding="utf-8")
    assert "## Verification Command" in body
    assert "python3 -m pytest -q" in body


def test_latest_failed_run(tmp_path, monkeypatch):
    home, db, _root = _make_env(tmp_path, monkeypatch)
    assert home.is_dir()
    assert latest_failed_run(str(db)) == RUN_ID


def test_verifier_node_finds_framework_traceback_in_evidence_log(tmp_path, monkeypatch):
    """Node ``test_verifier`` writes ``verifier-test.log`` + ``evidence/test-*.log``.

    Neither is named after the node id, so discovery must derive the stem; the
    traceback lives in the evidence log and must drive a ``mini_ork`` verdict.
    """
    root = tmp_path / "repo"
    (root / "recipes" / "code-fix" / "verifiers").mkdir(parents=True)
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / RUN_ID
    (run_dir / "evidence").mkdir(parents=True)
    (run_dir / "verifier-test.log").write_text("[checks] all ok\n", encoding="utf-8")
    (run_dir / "evidence" / "test-1790703693.log").write_text(
        'Traceback (most recent call last):\n'
        f'  File "{root}/recipes/code-fix/verifiers/test.py", line 298, in <module>\n'
        "TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'\n",
        encoding="utf-8",
    )

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    for ddl in (_RUN_EVENTS_DDL, _TASK_RUNS_DDL, _BUG_REPORTS_DDL, _EPICS_DDL):
        con.execute(ddl)
    con.execute(
        "INSERT INTO task_runs(id, task_class, recipe, status, updated_at) VALUES(?,?,?,?,?)",
        (RUN_ID, "framework_edit", "framework-edit", "failed", 1000),
    )
    con.execute(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json, finish_reason) "
        "VALUES(?,?,?,?,?)",
        ("ev1", RUN_ID, "node_end",
         json.dumps({"node_id": "test_verifier", "node_type": "verifier"}), "error"),
    )
    con.commit()
    con.close()

    failures = load_failures(str(db), RUN_ID, home=str(home))
    assert "TypeError" in failures[0].log_excerpt

    res = triage_run(RUN_ID, home=str(home), db=str(db), root=str(root), dry_run=True)
    assert res.blame == "mini_ork"


def test_kill_switch_blocks_writes(tmp_path, monkeypatch):
    """The ``.triage-kill`` sentinel stops filing without touching env vars.

    Diagnosis still works (blame is unchanged); only the write is suppressed —
    so an operator can silence the loop and still run ``--dry-run``.
    """
    home, db, root = _make_env(tmp_path, monkeypatch)
    (home / "state").mkdir()
    (home / "state" / ".triage-kill").write_text("", encoding="utf-8")

    res = triage_run(RUN_ID, home=str(home), db=str(db), root=str(root))
    assert res.blame == "mini_ork"
    assert res.bug_id is None
    assert "kill switch" in res.reason

    con = sqlite3.connect(db)
    count = con.execute("SELECT COUNT(*) FROM bug_reports").fetchone()[0]
    con.close()
    assert count == 0


def test_exercise_copy_records_defect_attribution(tmp_path, monkeypatch):
    """``db:V4`` closes only when the funnel itself writes an attribution row.

    G8 (``rsi.cycle._exercise_once``) re-asks ``db:V4`` after running this
    file's scoped tests against a copy of the measured DB; the rule needs both
    a reachable call site and a non-zero ``defect_attributions`` count. Seeding
    the table directly would fake that measurement, so this drives the real
    producer — ``triage_run`` — against G8's copy, with a failed run whose log
    names an existing, git-tracked repo file at a blameable line. Skips cleanly
    unless ``MINI_ORK_DB`` carries that copy's shape, so the ordinary suite
    stays hermetic (and never writes the operator's live DB).
    """
    db = _G8_DB
    if not _is_exercise_copy(db):
        pytest.skip("G8 did not supply an exercised measured-corpus DB")

    repo = Path(__file__).resolve().parents[2]
    run_id = "g8-defect-attribution-probe"
    task_class = "rsi-g8-attribution-probe"
    frame_rel = "mini_ork/triage/failures.py"  # tracked in the tree under test

    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "implementer.log").write_text(
        "Traceback (most recent call last):\n"
        f'  File "{repo / frame_rel}", line 1, in run_node\n'
        "RuntimeError: attribution probe\n",
        encoding="utf-8",
    )

    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT OR REPLACE INTO task_runs"
            "(id, task_class, recipe, kickoff_path, status, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (run_id, task_class, "framework-edit", "kickoff.md", "failed", 1000, 1000),
        )
        con.execute(
            "INSERT OR REPLACE INTO run_events"
            "(event_id, run_id, event_type, payload_json, finish_reason)"
            " VALUES(?,?,?,?,?)",
            (
                f"ev-{run_id}",
                run_id,
                "node_end",
                json.dumps({"node_id": "implementer", "node_type": "implementer"}),
                "error",
            ),
        )
        con.commit()
    finally:
        con.close()

    # ``bug_report`` resolves its own DB/home from ambient env; scope both to
    # the exercised copy so the funnel's post-hook cannot leak into the tree.
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(repo))

    res = triage_run(run_id, home=str(home), db=db, root=str(repo))
    assert res.blame == "mini_ork"

    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT found_run_id, task_class FROM defect_attributions"
            " WHERE found_run_id=?",
            (run_id,),
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1, f"expected exactly one attribution row, got {rows}"
    assert rows[0][0] == run_id
    assert rows[0][1] == task_class, "task_class must come from task_runs, not a default"
