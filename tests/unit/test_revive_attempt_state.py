"""RB-16: a revived run starts a clean attempt.

The dead attempt's verdict, end stamp, ``reaped:`` note, run-level verdict and
attempt markers must not leak into the attempt that follows. Every test here is
hermetic: a throwaway ``state.db`` with the ``task_runs`` shape, no ambient
``MINI_ORK_*`` / ``MO_*`` identity, and no network.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

import pytest

import mini_ork.dispatch.llm_dispatch as llm_dispatch_module
from mini_ork.cli.execute import _emit_run_verdict, set_status
from mini_ork.cli.publisher import publisher_node
from mini_ork.gates.rubric_prescreen import mo_rubric_run_score
from mini_ork.recovery.diagnose import LAYER_PROVIDER, diagnose
from mini_ork.web.control import _restore_status

RUN = "run-rb16"
REAPED = "reaped: dispatcher pid 96447 gone"
ATTEMPT_RE = re.compile(r"^attempt (\d+) started (.+)$")

_TASK_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
  id               TEXT PRIMARY KEY,
  task_class       TEXT NOT NULL,
  recipe           TEXT,
  workflow_version TEXT NOT NULL DEFAULT 'latest',
  kickoff_path     TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'classified',
  verdict          TEXT,
  cost_usd         REAL NOT NULL DEFAULT 0,
  duration_ms      INTEGER NOT NULL DEFAULT 0,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL,
  ended_at         INTEGER,
  notes            TEXT
)
"""


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("MINI_ORK_", "MO_")):
            monkeypatch.delenv(key, raising=False)
    yield


def _make_db(tmp_path, **overrides):
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(_TASK_RUNS_DDL)
        row = {
            "id": RUN,
            "task_class": "framework_edit",
            "recipe": "framework-edit",
            "workflow_version": "latest",
            "kickoff_path": str(tmp_path / "kickoff.md"),
            "status": "failed",
            "verdict": None,
            "cost_usd": 0.0,
            "duration_ms": 0,
            "created_at": 1762800000,
            "updated_at": 1762800000,
            "ended_at": None,
            "notes": None,
        }
        row.update(overrides)
        con.execute(
            "INSERT OR REPLACE INTO task_runs (%s) VALUES (%s)"
            % (", ".join(row), ", ".join("?" for _ in row)),
            tuple(row.values()),
        )
        con.commit()
    finally:
        con.close()
    return str(db)


def _column(db, column):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            f"SELECT {column} FROM task_runs WHERE id = ?", (RUN,)).fetchone()[0]
    finally:
        con.close()


def _segments(notes):
    return [seg for seg in (notes or "").split("; ") if seg.strip()]


# ── attempt boundary: set_status / _restore_status ──────────────────────────


def test_live_status_write_clears_verdict_and_marks_attempt(tmp_path):
    db = _make_db(tmp_path, status="published", verdict="CRASH",
                  ended_at=1762968000, duration_ms=2880000, notes=REAPED)
    set_status(db, RUN, "executing")
    assert _column(db, "verdict") is None
    assert _column(db, "ended_at") is None
    assert _column(db, "duration_ms") == 0
    segments = _segments(_column(db, "notes"))
    assert REAPED in segments  # history is appended to, never erased
    match = ATTEMPT_RE.match(segments[-1])
    assert match, segments
    assert match.group(1) == "2", segments


def test_attempt_number_advances_one_per_revive(tmp_path):
    db = _make_db(tmp_path, status="published", verdict="CRASH",
                  ended_at=1762968000, duration_ms=2880000, notes=REAPED)
    set_status(db, RUN, "executing")
    first = ATTEMPT_RE.match(_segments(_column(db, "notes"))[-1])
    assert first
    # The revived attempt dies exactly as the reaper leaves it.
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE task_runs SET status = 'failed', verdict = 'CRASH', ended_at = ?, "
        "duration_ms = 5000, notes = COALESCE(notes || '; ', '') || ? WHERE id = ?",
        (1762970000, "reaped: dispatcher pid 99100 gone", RUN))
    con.commit()
    con.close()
    set_status(db, RUN, "executing")
    second = ATTEMPT_RE.match(_segments(_column(db, "notes"))[-1])
    assert second, _segments(_column(db, "notes"))
    assert int(second.group(1)) == int(first.group(1)) + 1
    assert _column(db, "verdict") is None


def test_restore_status_live_is_an_attempt_boundary(tmp_path):
    db = _make_db(tmp_path, status="not_needed", verdict="CRASH",
                  ended_at=1762968000, duration_ms=2880000, notes=REAPED)
    _restore_status(db, RUN, "executing")
    assert _column(db, "verdict") is None
    assert _column(db, "ended_at") is None
    segments = _segments(_column(db, "notes"))
    assert REAPED in segments
    assert ATTEMPT_RE.match(segments[-1]), segments


def test_restore_status_terminal_is_not_an_attempt_boundary(tmp_path):
    db = _make_db(tmp_path, status="not_needed", verdict="CRASH",
                  ended_at=1762968000, duration_ms=2880000, notes=REAPED)
    _restore_status(db, RUN, "failed")
    assert _column(db, "verdict") == "CRASH"
    assert _column(db, "notes") == REAPED
    assert _column(db, "ended_at") == 1762968000


# ── diagnose reads only notes after the last attempt marker ─────────────────


def test_diagnose_ignores_a_dead_attempts_reaped_note(tmp_path):
    _make_db(tmp_path, status="failed",
             notes=f"{REAPED}; attempt 2 started 2026-10-20T12:00:00Z")
    (tmp_path / "runs" / RUN).mkdir(parents=True)
    result = diagnose(tmp_path, RUN, write=False)
    assert not (result.get("rule_id") == "RN"
                and result.get("layer") == LAYER_PROVIDER), result
    assert "dispatcher" not in str(result.get("summary") or "").lower(), result


def test_diagnose_still_reads_a_reaped_note_after_the_last_marker(tmp_path):
    _make_db(tmp_path, status="failed",
             notes=(f"attempt 1 started 2026-10-20T11:00:00Z; {REAPED}; "
                    "attempt 2 started 2026-10-20T12:00:00Z; "
                    "reaped: dispatcher pid 98100 gone"))
    (tmp_path / "runs" / RUN).mkdir(parents=True)
    result = diagnose(tmp_path, RUN, write=False)
    assert result.get("rule_id") == "RN", result
    assert result.get("layer") == LAYER_PROVIDER, result
    assert "dispatcher" in str(result.get("summary") or "").lower(), result


# ── run-level verdict: recovery overwrites, normal execution does not ───────


_STALE_VERDICT = (
    '{"verdict": "fail", "failed_nodes": 1, "dispatched": 2, '
    '"source": "execute@run-level"}\n')


def test_recovery_overwrites_the_run_level_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")
    run_dir = tmp_path / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "verdict.json").write_text(_STALE_VERDICT)
    _emit_run_verdict(str(run_dir), 0, 2, recovery=True)
    data = json.loads((run_dir / "verdict.json").read_text())
    assert data.get("verdict") == "pass", data


def test_normal_execution_keeps_the_write_once_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")
    run_dir = tmp_path / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "verdict.json").write_text(_STALE_VERDICT)
    _emit_run_verdict(str(run_dir), 0, 2)
    data = json.loads((run_dir / "verdict.json").read_text())
    assert data.get("verdict") == "fail", data
    assert data.get("failed_nodes") == 1, data
    assert not (run_dir / "run-verdict.json").exists()


# ── publish archives the dead attempt's markers ─────────────────────────────


def test_publish_moves_stale_attempt_markers(tmp_path, monkeypatch):
    for var, off in (("MO_ORACLE_GATES_AUTO", "0"), ("MO_LEVEL_VECTOR", "0"),
                     ("MO_PROBE_VALIDITY", "0"), ("MO_EVIDENCE_LEDGER", "0"),
                     ("MO_KICKOFF_GUARD", "off")):
        monkeypatch.setenv(var, off)
    db = _make_db(tmp_path, status="reviewing")
    run_dir = tmp_path / "runs" / RUN
    run_dir.mkdir(parents=True)
    stale = {
        "NEEDS-CHANGE.md": "# NEEDS CHANGE\n\nthe dead attempt's narrative\n",
        "retry-hint.json": '{"from_node": "implementer"}\n',
        "retry-gate.json": '{"gate": "retry"}\n',
    }
    for name, body in stale.items():
        (run_dir / name).write_text(body)
    (run_dir / "verdict.json").write_text('{"verdict": "pass"}\n')

    rc, _reason = publisher_node(
        root=str(tmp_path), run_dir=str(run_dir), db=db, run_id=RUN,
        recipe="", task_class="generic")

    assert rc == 0
    assert _column(db, "status") == "published"
    for name, body in stale.items():
        assert not (run_dir / name).exists(), name
        moved = sorted((run_dir / "attempts").glob(f"*/{name}"))
        assert moved, name
        assert moved[0].read_text() == body
        assert moved[0].parent.name.isdigit(), moved[0]
    assert (run_dir / "verdict.json").exists()


# ── prescreen refreshes its own top-level keys ──────────────────────────────


def _prescreen(tmp_path, monkeypatch, existing):
    root = tmp_path / "root"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "rubric-prescreen.md").write_text(
        "{{KICKOFF_BODY}}\n{{DIFF_SUMMARY}}\n")
    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("# kickoff\n")
    run_dir = tmp_path / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "panel-verdict.json").write_text(json.dumps(existing))

    def fake_dispatch(argv, root=None, **kwargs):
        print('{"pass": true, "score": 6}')
        return 0

    monkeypatch.setattr(llm_dispatch_module, "llm_dispatch", fake_dispatch)
    mo_rubric_run_score(
        str(kickoff), str(run_dir), "generic",
        mini_ork_root=str(root), mini_ork_home=str(tmp_path),
        mini_ork_db=str(tmp_path / "state.db"))
    return json.loads((run_dir / "panel-verdict.json").read_text())


def test_prescreen_rerun_updates_top_level_score_and_pass(tmp_path, monkeypatch):
    data = _prescreen(tmp_path, monkeypatch, {
        "panel_score": 37.5, "pass": False, "source": "rubric-prescreen",
        "task_class": "generic", "scale": "rubric 0-8 mapped to 0-100",
    })
    assert data.get("panel_score") == 75.0, data
    assert data.get("pass") is True, data


def test_prescreen_rerun_keeps_a_real_panel_verdict(tmp_path, monkeypatch):
    data = _prescreen(tmp_path, monkeypatch, {
        "verdict": "ESCALATE", "reasons": ["needs change"],
        "dod_probe_results": [], "panel_score": 37.5, "pass": False,
        "source": "tier4_synth",
    })
    assert data.get("panel_score") == 37.5, data
    assert data.get("pass") is False, data
    assert data.get("verdict") == "ESCALATE", data


def test_review_park_does_not_start_a_new_attempt(tmp_path):
    """Review fix (coordinator): only ``executing`` opens an attempt; a
    ``reviewing`` park inside the same pass appends no marker."""
    import sqlite3
    from mini_ork.cli.execute import set_status
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT, verdict TEXT, "
                "notes TEXT, ended_at INTEGER, duration_ms INTEGER NOT NULL DEFAULT 0, "
                "updated_at INTEGER, created_at INTEGER)")
    con.execute("INSERT INTO task_runs (id, status, created_at) VALUES ('r1', 'planned', 0)")
    con.commit(); con.close()
    set_status(str(db), "r1", "executing")
    set_status(str(db), "r1", "reviewing")
    notes = sqlite3.connect(db).execute("SELECT notes FROM task_runs WHERE id='r1'").fetchone()[0]
    assert notes.count("attempt ") == 1 and notes.startswith("attempt 1 started")
