"""The repair-loop half of the diagnosis ledger (RSI Tier-0 #2).

Every diagnosis ``auto_repair.apply`` acts on with a spawnable action now gets a
``diagnosis_ledger`` row (not just the ``escalate`` ones), and the supervisor's
R2 labels that row once the repaired run reaches a terminal status.

Hermetic: a temp mini-ork home with a ``runs/<run_id>/`` directory and a
``state.db`` holding the ``task_runs`` columns ``_task_run`` reads. No network,
no spawn (``apply(..., spawn=False)``), no real engine checkout.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.recovery import auto_repair
from mini_ork.recovery import supervisor
from mini_ork.stores import diagnosis_ledger

RUN_ID = "run-diag-ledger-repair-1"
OTHER_RUN_ID = "run-diag-ledger-repair-2"


def _dx(**overrides) -> dict:
    dx = {"source": "model", "layer": "provider", "routes_to": "lane",
          "confidence": 0.7, "rule_id": "model", "stage": "execute",
          "node": "implementer", "summary": "s"}
    dx.update(overrides)
    return dx


def _decision(action: str = "lane", *, diagnosis=_dx(), **overrides) -> dict:
    decision = {"action": action, "from_node": "implementer", "lanes": {},
                "reason": "r", "signature": "sig-1"}
    if diagnosis is not None:
        decision["diagnosis"] = diagnosis
    decision.update(overrides)
    return decision


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A temp mini-ork home: ``runs/<run_id>/`` plus a minimal ``state.db``."""
    h = tmp_path / "home"
    (h / "runs" / RUN_ID).mkdir(parents=True)
    (h / "engine").mkdir()
    con = sqlite3.connect(str(h / "state.db"))
    con.execute(
        "CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT,"
        " updated_at INTEGER, kickoff_path TEXT, created_at INTEGER,"
        " cost_usd REAL, notes TEXT)")
    con.commit()
    con.close()
    return h


def _seed(home: Path, *, status: str = "failed", run_id: str = RUN_ID,
          updated_at: int | None = None, with_row: bool = True) -> None:
    """Seed the ``task_runs`` row the supervisor reads (and the run dir)."""
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)
    if not with_row:
        return
    con = sqlite3.connect(str(home / "state.db"))
    try:
        con.execute(
            "INSERT OR REPLACE INTO task_runs (id, status, updated_at)"
            " VALUES (?,?,?)",
            (run_id, status,
             int(updated_at if updated_at is not None else time.time())))
        con.commit()
    finally:
        con.close()


def _set_run(home: Path, *, status: str, updated_at: int,
             run_id: str = RUN_ID) -> None:
    con = sqlite3.connect(str(home / "state.db"))
    try:
        con.execute(
            "INSERT OR REPLACE INTO task_runs (id, status, updated_at)"
            " VALUES (?,?,?)", (run_id, status, int(updated_at)))
        con.commit()
    finally:
        con.close()


def _read_repair(home: Path, run_id: str = RUN_ID) -> dict:
    return json.loads(
        (home / "runs" / run_id / "repair.json").read_text(encoding="utf-8"))


def _ledger_rows(home: Path) -> list[tuple]:
    con = sqlite3.connect(str(home / "state.db"))
    try:
        return con.execute(
            "SELECT id, run_id, source, action, label FROM diagnosis_ledger"
            " ORDER BY id").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _sweep(home: Path, *, dry_run: bool = False) -> list[dict]:
    return supervisor.sweep(str(home), str(home / "state.db"),
                            engine_root=str(home / "engine"), dry_run=dry_run)


def _label_actions(actions: list[dict],
                   run_id: str = RUN_ID) -> list[dict]:
    return [a for a in actions
            if a.get("run_id") == run_id and a.get("action") == "label"]


def _record_and_point_run(home: Path, *, status: str,
                          updated_at_delta: int = 100) -> int:
    """Drive a lane repair through ``apply``, then set the run's terminal
    status with ``updated_at`` after ``ledger_recorded_at``."""
    auto_repair.apply(home, RUN_ID, _decision(), spawn=False)
    recorded_at = int(_read_repair(home)["ledger_recorded_at"])
    _set_run(home, status=status, updated_at=recorded_at + updated_at_delta)
    return recorded_at


# ── the helper ───────────────────────────────────────────────────────────────


def test_record_repair_ledger_records_a_row(home):
    assert auto_repair._record_repair_ledger(home, RUN_ID, _dx(), "lane") is True
    rows = _ledger_rows(home)
    assert len(rows) == 1
    _id, run_id, source, action, label = rows[0]
    assert (run_id, source, action, label) == (RUN_ID, "model", "lane", None)


def test_record_repair_ledger_call_shape(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_record(db, run_id, diagnosis, *, action, fix_epic_id=None):
        seen.update(db=str(db), run_id=run_id, diagnosis=diagnosis,
                    action=action)
        return 11

    monkeypatch.setattr(diagnosis_ledger, "record", fake_record)
    dx = _dx()
    assert auto_repair._record_repair_ledger(tmp_path, RUN_ID, dx, "revise")
    assert seen == {"db": str(tmp_path / "state.db"), "run_id": RUN_ID,
                    "diagnosis": dx, "action": "revise"}


def test_record_repair_ledger_truthy_id_only(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnosis_ledger, "record", lambda *a, **k: 3)
    assert auto_repair._record_repair_ledger(tmp_path, RUN_ID, _dx(), "lane")
    monkeypatch.setattr(diagnosis_ledger, "record", lambda *a, **k: 0)
    assert not auto_repair._record_repair_ledger(tmp_path, RUN_ID, _dx(), "lane")


def test_record_repair_ledger_never_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(diagnosis_ledger, "record", boom)
    assert auto_repair._record_repair_ledger(
        tmp_path, RUN_ID, _dx(), "lane") is False


def test_diagnosis_ledger_is_imported_locally():
    assert not hasattr(auto_repair, "diagnosis_ledger")


# ── the apply call site ──────────────────────────────────────────────────────


def test_apply_records_row_and_arms_r2(home):
    result = auto_repair.apply(home, RUN_ID, _decision(), spawn=False)
    assert result["action"] == "lane"
    rows = _ledger_rows(home)
    assert len(rows) == 1
    _id, run_id, source, action, label = rows[0]
    assert (run_id, source, action, label) == (RUN_ID, "model", "lane", None)
    repair = _read_repair(home)
    assert isinstance(repair.get("ledger_recorded_at"), int)
    assert "labelled_at" not in repair


def test_apply_records_row_when_spawn_true(home, monkeypatch):
    monkeypatch.setattr(
        auto_repair, "_spawn_recover",
        lambda *a, **k: {"spawned": True, "pid": 4321, "log": "",
                         "command": "x"})
    auto_repair.apply(home, RUN_ID, _decision(), spawn=True)
    rows = _ledger_rows(home)
    assert len(rows) == 1
    assert (rows[0][2], rows[0][3]) == ("model", "lane")
    assert isinstance(_read_repair(home).get("ledger_recorded_at"), int)


@pytest.mark.parametrize("action", ["lane", "infra", "reverify", "revise",
                                    "prove"])
def test_apply_records_every_spawnable_action(home, action):
    auto_repair.apply(home, RUN_ID, _decision(action), spawn=False)
    rows = _ledger_rows(home)
    assert len(rows) == 1
    assert (rows[0][1], rows[0][2], rows[0][3]) == (RUN_ID, "model", action)


def test_apply_no_source_writes_no_row(home):
    auto_repair.apply(home, RUN_ID, _decision(diagnosis=_dx(source="")),
                      spawn=False)
    assert _ledger_rows(home) == []
    repair = _read_repair(home)
    assert "ledger_recorded_at" not in repair
    assert [a["action"] for a in repair["attempts"]] == ["lane"]


def test_apply_non_dict_diagnosis_writes_no_row(home):
    auto_repair.apply(home, RUN_ID, _decision(diagnosis=None), spawn=False)
    assert _ledger_rows(home) == []
    assert "ledger_recorded_at" not in _read_repair(home)


def test_apply_escalate_untouched(home):
    dx = _dx(layer="engine", routes_to="fix_epic")
    auto_repair.apply(home, RUN_ID, _decision("escalate", diagnosis=dx),
                      spawn=False)
    assert "ledger_recorded_at" not in _read_repair(home)
    # Any row the escalation path writes itself is out of this path's scope.
    for _id, _run, _source, action, _label in _ledger_rows(home):
        assert action in ("human", "escalate")


def test_ledger_failure_never_fails_the_repair(home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(diagnosis_ledger, "record", boom)
    result = auto_repair.apply(home, RUN_ID, _decision(), spawn=False)
    assert result["action"] == "lane"
    repair = _read_repair(home)
    assert [a["action"] for a in repair["attempts"]] == ["lane"]
    assert "ledger_recorded_at" not in repair
    assert auto_repair._record_repair_ledger(
        home, RUN_ID, _dx(), "lane") is False


def test_apply_pops_a_stale_label(home):
    (home / "runs" / RUN_ID / "repair.json").write_text(json.dumps({
        "state": "revived", "labelled_at": 1000, "label_correct": True,
        "attempts": [],
    }), encoding="utf-8")
    auto_repair.apply(home, RUN_ID, _decision(), spawn=False)
    repair = _read_repair(home)
    assert "labelled_at" not in repair
    assert isinstance(repair.get("ledger_recorded_at"), int)


def test_apply_records_no_row_for_a_human_hand_off(home):
    auto_repair.apply(home, RUN_ID, _decision("human"), spawn=False)
    assert _ledger_rows(home) == []
    assert "ledger_recorded_at" not in _read_repair(home)


# ── R2 labels the repaired outcome ───────────────────────────────────────────


def test_published_run_is_labelled_right(home):
    _record_and_point_run(home, status="published")
    state_before = _read_repair(home)["state"]
    actions = _sweep(home)
    assert [row[4] for row in _ledger_rows(home)] == [1]
    repair = _read_repair(home)
    assert isinstance(repair.get("labelled_at"), int)
    assert repair.get("label_correct") is True
    assert repair["state"] == state_before
    labelled = _label_actions(actions)
    assert labelled == [{"run_id": RUN_ID, "action": "label",
                         "reason": "repaired run published — diagnosis was right",
                         "correct": True}]


def test_failed_run_is_labelled_wrong(home):
    _record_and_point_run(home, status="failed")
    actions = _sweep(home)
    assert [row[4] for row in _ledger_rows(home)] == [0]
    repair = _read_repair(home)
    assert isinstance(repair.get("labelled_at"), int)
    assert repair.get("label_correct") is False
    assert repair["state"] == "repairing"
    (labelled,) = _label_actions(actions)
    assert labelled["correct"] is False
    assert labelled["reason"] == "repaired run failed — diagnosis was wrong"


@pytest.mark.parametrize("status", ["superseded", "not_needed"])
def test_unjudgeable_outcome_is_not_labelled(home, status):
    # Review fix: a superseded / not-needed run's work landed elsewhere, so its
    # outcome is no evidence about the diagnosis — a "wrong" label would bias
    # the calibrated escalation bar. The row stays unlabelled; the marker is
    # stamped so the rule does not re-check the run every sweep.
    _record_and_point_run(home, status=status)
    actions = _sweep(home)
    assert [row[4] for row in _ledger_rows(home)] == [None]
    repair = _read_repair(home)
    assert isinstance(repair.get("labelled_at"), int)
    assert repair.get("label_skipped") == status
    assert "label_correct" not in repair
    assert _label_actions(actions) == []
    skipped = [a for a in actions if a.get("action") == "label_skipped"]
    assert skipped == [{"run_id": RUN_ID, "action": "label_skipped",
                        "reason": f"repaired run {status} — diagnosis not judged"}]
    assert not [a for a in _sweep(home) if a.get("run_id") == RUN_ID]


def test_no_label_while_running(home):
    _record_and_point_run(home, status="running")
    actions = _sweep(home)
    assert [row[4] for row in _ledger_rows(home)] == [None]
    assert "labelled_at" not in _read_repair(home)
    assert not any(a.get("run_id") == RUN_ID for a in actions)


def test_no_label_when_updated_at_is_not_after_recording(home):
    _record_and_point_run(home, status="published", updated_at_delta=0)
    actions = _sweep(home)
    assert [row[4] for row in _ledger_rows(home)] == [None]
    assert "labelled_at" not in _read_repair(home)
    assert not _label_actions(actions)


def test_missing_run_row_is_not_labelled(home):
    auto_repair.apply(home, RUN_ID, _decision(), spawn=False)
    actions = _sweep(home)
    assert [row[4] for row in _ledger_rows(home)] == [None]
    assert "labelled_at" not in _read_repair(home)
    assert not any(a.get("run_id") == RUN_ID for a in actions)


def test_dry_run_writes_nothing(home):
    _record_and_point_run(home, status="published")
    before = (home / "runs" / RUN_ID / "repair.json").read_text(encoding="utf-8")
    actions = _sweep(home, dry_run=True)
    assert [row[4] for row in _ledger_rows(home)] == [None]
    assert (home / "runs" / RUN_ID / "repair.json").read_text(
        encoding="utf-8") == before
    (labelled,) = _label_actions(actions)
    assert labelled["correct"] is True


def test_second_sweep_is_quiet(home):
    _record_and_point_run(home, status="published")
    assert _label_actions(_sweep(home))
    assert not _label_actions(_sweep(home))
    assert [row[4] for row in _ledger_rows(home)] == [1]


# ── the revived path is untouched ────────────────────────────────────────────


def test_revived_path_unchanged(home):
    revived_at = int(time.time()) - 1000
    _seed(home, status="published", updated_at=revived_at + 500)
    (home / "runs" / RUN_ID / "repair.json").write_text(json.dumps({
        "state": "reviving", "revived_at": revived_at,
        "revive_from_node": "implementer", "fix_epic_id": "epic-1",
        "attempts": [],
    }), encoding="utf-8")
    diagnosis_ledger.record(home / "state.db", RUN_ID, _dx(),
                            action="escalate", fix_epic_id="epic-1")
    (labelled,) = _label_actions(_sweep(home))
    assert labelled["reason"] == "revived run published — diagnosis was right"
    repair = _read_repair(home)
    assert repair.get("label_correct") is True
    assert repair["state"] == "revived"
    assert [row[4] for row in _ledger_rows(home)] == [1]


def test_revived_marker_wins_over_ledger_marker(home):
    revived_at = int(time.time()) - 1000
    _seed(home, status="published", updated_at=revived_at + 500)
    (home / "runs" / RUN_ID / "repair.json").write_text(json.dumps({
        "state": "reviving", "revived_at": revived_at,
        "revive_from_node": "implementer", "fix_epic_id": "epic-1",
        "ledger_recorded_at": int(time.time()) - 100,
        "attempts": [],
    }), encoding="utf-8")
    diagnosis_ledger.record(home / "state.db", RUN_ID, _dx(),
                            action="escalate", fix_epic_id="epic-1")
    (labelled,) = _label_actions(_sweep(home))
    assert labelled["reason"].startswith("revived run")
