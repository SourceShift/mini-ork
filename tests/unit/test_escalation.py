"""``mini_ork.recovery.escalation`` + the ``escalate`` action (SDE-3a).

Hermetic: a temp mini-ork home with a hand-rolled sqlite schema (mirroring
``test_triage_failures_py.py``), a stubbed retry hint and a stubbed ``diagnose``
(the diagnosis is an *input* to this wiring, and ``test_diagnose.py`` owns its
rule set). No LLM, no network, no spawn — ``subprocess.Popen`` is stubbed so a
recover would be visible if one were ever launched.

The motivating shape is ``run-1791914481-48337``: an engine fault at
``deliver/publisher`` with ``routes_to: fix_epic`` and no traceback — the case
``triage_run`` cannot file (it re-decides blame from traceback frames) and
``auto_repair.decide`` used to hand to a human.

The parked half (SDE-3a pass 2): once a run is in ``waiting_on_fix`` the fix
epic owns the failure, so ``decide`` returns ``none`` citing it and ``apply`` /
``maybe_repair`` leave ``repair.json`` byte-identical and spawn nothing.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from mini_ork.recovery import auto_repair as ar
from mini_ork.recovery import escalation as esc
from mini_ork.recovery.escalation_policy import should_escalate
from mini_ork.triage import failures as tf

RUN = "run-1791914481-48337"

_TASK_RUNS_DDL = """
CREATE TABLE task_runs (
  id TEXT PRIMARY KEY,
  recipe TEXT,
  status TEXT,
  task_class TEXT,
  cost_usd REAL DEFAULT 0,
  kickoff_path TEXT,
  workflow_version TEXT,
  trace_id TEXT,
  created_at INTEGER,
  updated_at INTEGER,
  ended_at INTEGER,
  notes TEXT
)
"""

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

# No CHECK on ``status``: the escalated/failed/abandoned vocabulary is the
# point, and the production CHECK does not carry all three.
_EPICS_DDL = """
CREATE TABLE epics (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  kickoff_path TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at TEXT
)
"""

_MO_INBOX_DDL = """
CREATE TABLE mo_inbox_gates (
  inbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
  gate_id TEXT NOT NULL,
  feature TEXT NOT NULL,
  phase TEXT,
  context_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  review_note TEXT,
  enqueued_at INTEGER NOT NULL,
  resolved_at INTEGER,
  blocks_dispatch_for TEXT
)
"""

# R1 as ``diagnose`` emits it: a withheld publish whose levels were proven, so
# the engine could not deliver the change.
ENGINE_DIAGNOSIS = {
    "version": 2,
    "run_id": RUN,
    "rule_id": "R1",
    "layer": "engine",
    "stage": "deliver",
    "node": "publisher",
    "source": "rule",
    "confidence": 0.9,
    "evidence": [{"kind": "finish_reason", "ref": "publish_abstain",
                  "excerpt": "the publisher withheld the publish although levels were proven"}],
    "routes_to": "fix_epic",
    "summary": "the publisher withheld the publish at publisher although every level was proven",
    "computed_at": "2026-10-15T00:00:00Z",
}

_CALLS: list[list[str]] = []


class _FakePopen:
    """Satisfies both ``Popen`` and ``subprocess.run`` (which calls ``Popen``)."""

    def __init__(self, argv, **kw):
        self.argv = list(argv)
        self.args = self.argv
        self.pid = 4242
        self.returncode = 0
        self.stdout = None
        self.stderr = None
        _CALLS.append(self.argv)

    def communicate(self, input=None, timeout=None):
        return b"", b""

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def spawned(monkeypatch):
    _CALLS.clear()
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _CALLS


def _recover_spawns(calls):
    return [c for c in calls if "mini_ork.recovery.planner" in c]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A temp home with the schema, a failed run row and the run dir."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": "chapter-review"}))

    db = home / "state.db"
    con = sqlite3.connect(db)
    for ddl in (_TASK_RUNS_DDL, _RUN_EVENTS_DDL, _BUG_REPORTS_DDL, _EPICS_DDL, _MO_INBOX_DDL):
        con.execute(ddl)
    con.execute(
        "INSERT INTO task_runs(id, recipe, status, cost_usd, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (RUN, "chapter-review", "failed", 0.5, 1791914481, 1791914500),
    )
    con.commit()
    con.close()

    for key, value in (
        ("MINI_ORK_HOME", str(home)),
        ("MINI_ORK_DB", str(db)),
        ("MINI_ORK_ROOT", str(root)),
        # tests/conftest.py disables auto-repair suite-wide.
        ("MO_AUTO_REPAIR", "1"),
    ):
        monkeypatch.setenv(key, value)
    for key in ("MO_FIX_EPIC_DAILY_CAP", "MO_FIX_EPIC_BREAKER_N", "MO_AUTO_ESCALATE"):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture(autouse=True)
def _stub_policy_inputs(monkeypatch):
    """The retry hint and the diagnosis are inputs to this wiring, not subjects."""
    import mini_ork.recovery.retry_hint as rh
    monkeypatch.setattr(rh, "_recipe_workflow", lambda home, recipe: ([], []))
    monkeypatch.setattr(rh, "load_or_compute", lambda *a, **k: {
        "version": 2, "run_id": RUN, "failed_node": None, "retryable": True,
        "strategy": "none", "from_node": None, "needs_change": None,
        "notes": [], "command": "", "computed_at": "2026-01-01T00:00:00Z",
    })


def _stub_diagnosis(monkeypatch, diagnosis: dict | None) -> None:
    import mini_ork.recovery.diagnose as dx
    monkeypatch.setattr(
        dx, "diagnose",
        lambda home, run_id, write=False: (dict(diagnosis) if diagnosis else None))


def _diag(**over) -> dict:
    return {**ENGINE_DIAGNOSIS, **over}


# ── DB helpers ───────────────────────────────────────────────────────────────


def _rows(home: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _count(home: Path, table: str) -> int:
    return int(_rows(home, f"SELECT COUNT(*) FROM {table}")[0][0])  # noqa: S608 — test-only


def _seed_self_driving_epic(home: Path, *, fingerprint: str, status: str = "escalated",
                            bug_id_hint: int = 0, age_s: int = 0,
                            created_at: str = "2026-10-14T00:00:00Z") -> str:
    """A promoted fix epic this loop opened — the cap/breaker/GRAO inputs."""
    now = int(time.time())
    bug_id = 5000 + bug_id_hint
    epic_id = f"bug-{bug_id}-seed{bug_id_hint}"
    con = sqlite3.connect(home / "state.db")
    try:
        con.execute(
            "INSERT INTO bug_reports(fingerprint, run_id, agent_role, observed_in, title, "
            "description, severity, confidence, frequency, status, promoted_to_epic_id, "
            "first_seen_at, last_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,1,'queued_as_epic',?,?,?,?)",
            (fingerprint, "run-other", "self-driving", "/tmp", f"seed {bug_id_hint}",
             "", "high", 0.9, epic_id, now - age_s, now - age_s, now - age_s),
        )
        con.execute(
            "INSERT INTO epics(id, title, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (epic_id, f"seed epic {bug_id_hint}", status, created_at, created_at),
        )
        con.commit()
    finally:
        con.close()
    return epic_id


def _read_repair(home: Path) -> dict:
    return json.loads((home / "runs" / RUN / "repair.json").read_text())


# ── the motivating case: decide routes, apply files and parks ────────────────


def test_decide_escalates_engine_fix_epic(home, monkeypatch):
    _stub_diagnosis(monkeypatch, ENGINE_DIAGNOSIS)
    decision = ar.decide(home, RUN)
    assert decision["action"] == "escalate"
    assert decision["diagnosis"]["layer"] == "engine"
    assert decision["signature"]
    # ``escalate`` must NOT be spawnable — it parks the run, it does not resume it.
    assert "escalate" not in ar._SPAWNABLE
    assert decision["stop"] is False


def test_apply_escalate_files_epic_parks_run_and_notifies(home, monkeypatch, spawned):
    _stub_diagnosis(monkeypatch, ENGINE_DIAGNOSIS)
    decision = ar.decide(home, RUN)
    result = ar.apply(home, RUN, decision)

    assert result["escalated"] is True
    assert result["action"] == "escalate"
    assert result["fix_epic_id"]
    assert result["fix_bug_id"] is not None

    # Exactly one self-driving bug, attributed from the diagnosis.
    bugs = _rows(home, "SELECT agent_role, severity, confidence, observed_in, title, "
                       "status, promoted_to_epic_id FROM bug_reports")
    assert len(bugs) == 1
    role, severity, confidence, observed_in, title, status, epic_id = bugs[0]
    assert role == "self-driving"
    assert severity == "high"
    assert confidence == pytest.approx(0.9)
    assert observed_in == str(home / "runs" / RUN)
    assert title.startswith("[engine/deliver] publisher:")
    assert status == "queued_as_epic"
    assert epic_id == result["fix_epic_id"]

    # Exactly one framework-edit epic, with the auto-triage origin stamp.
    epics = _rows(home, "SELECT id, recipe, status FROM epics")
    assert len(epics) == 1
    assert epics[0][0] == result["fix_epic_id"]
    assert epics[0][1] == "framework-edit"
    assert Path(result["kickoff_path"]).read_text(encoding="utf-8").startswith(
        "<!-- mini-ork:origin=auto-triage -->")

    # The run is parked on the fix, never marked repairing/gave_up.
    repair = _read_repair(home)
    assert repair["state"] == "waiting_on_fix"
    assert repair["fix_epic_id"] == result["fix_epic_id"]
    assert repair["fix_bug_id"] == result["fix_bug_id"]
    assert repair["triaged"] is True
    assert [a["action"] for a in repair["attempts"]] == ["escalate"]

    # The decision is logged, and the oversight item is one non-blocking notice.
    events = _rows(home, "SELECT event_type, payload_json FROM run_events "
                         "WHERE event_type=?", (esc.ESCALATE_EVENT,))
    assert len(events) == 1
    payload = json.loads(events[0][1])
    assert payload["decision"]["escalate"] is True
    assert payload["fix_epic_id"] == result["fix_epic_id"]

    items = _rows(home, "SELECT gate_id, feature, status, blocks_dispatch_for, context_json "
                        "FROM mo_inbox_gates")
    assert len(items) == 1
    assert items[0][0] == "self-driving-escalation"
    assert items[0][1] == "fix-epic"
    assert items[0][2] == "pending"
    assert items[0][3] in ("", None)  # non-blocking
    ctx = json.loads(items[0][4])
    assert ctx["run_id"] == RUN and ctx["fix_epic_id"] == result["fix_epic_id"]

    # No recover: the run waits for its fix epic.
    assert _recover_spawns(spawned) == []


def test_escalate_twice_reuses_the_bug_row_and_the_epic(home, monkeypatch, spawned):
    """Re-filing the same fingerprint reuses the stored row — no inflation."""
    first = esc.escalate(home, RUN, ENGINE_DIAGNOSIS)
    second = esc.escalate(home, RUN, ENGINE_DIAGNOSIS)

    assert first["escalated"] is True and second["escalated"] is True
    assert first["fix_epic_id"] == second["fix_epic_id"]
    assert first["fix_bug_id"] == second["fix_bug_id"]
    assert _count(home, "bug_reports") == 1
    assert _count(home, "epics") == 1
    assert _rows(home, "SELECT frequency FROM bug_reports")[0][0] == 1
    assert _recover_spawns(spawned) == []
    repair = _read_repair(home)
    assert repair["state"] == "waiting_on_fix"
    assert repair["triaged"] is True
    # Both decisions were logged, under distinct event ids.
    assert _rows(home, "SELECT COUNT(DISTINCT event_id) FROM run_events WHERE event_type=?",
                 (esc.ESCALATE_EVENT,))[0][0] == 2


def test_parked_run_is_inert_to_auto_repair(home, monkeypatch, spawned):
    """Once parked on a fix epic, the loop does NOTHING to the run.

    ``escalation.escalate`` recorded ``waiting_on_fix`` + the epic, so the epic
    owns the failure now. Every later pass over the same failure — the sweep,
    the recovered run's own hook, a re-decide — must therefore be a pure no-op:
    ``decide`` returns ``none`` citing the epic, and ``apply`` leaves
    ``repair.json`` byte-identical (no attempt appended, no state flip, no run
    event) and spawns nothing. Pass 1 kept the stop rules ahead of the escalate
    rule and recorded a second same-signature pass as a "no progress" give-up —
    which is the wrong verdict for a run that is waiting, not stalling, and
    which also drifted the attempt count on every sweep.
    """
    _stub_diagnosis(monkeypatch, ENGINE_DIAGNOSIS)
    first = ar.apply(home, RUN, ar.decide(home, RUN))
    assert first["escalated"] is True
    assert _read_repair(home)["state"] == "waiting_on_fix"

    repair_path = home / "runs" / RUN / "repair.json"
    before = repair_path.read_bytes()
    events_before = _count(home, "run_events")

    decision = ar.decide(home, RUN)
    assert decision["action"] == "none"
    assert decision["stop"] is False
    assert decision["fix_epic_id"] == first["fix_epic_id"]
    assert "waiting on fix epic" in decision["reason"]
    assert first["fix_epic_id"] in decision["reason"]

    result = ar.apply(home, RUN, decision)
    assert result["action"] == "none"
    assert result["parked"] is True
    assert result["fix_epic_id"] == first["fix_epic_id"]
    assert repair_path.read_bytes() == before  # byte-identical: nothing written

    # ``maybe_repair`` is the production entry point for that decision: a none
    # decision reaches it too, and must leave the record alone (`_mark_repaired_if_done`
    # is gated on published/done, so a still-failed parked run is never rewritten).
    assert ar.maybe_repair(home, RUN) is not None
    assert repair_path.read_bytes() == before

    # Nothing re-filed, nothing re-notified, nothing spawned.
    assert _count(home, "run_events") == events_before
    assert _count(home, "bug_reports") == 1
    assert _count(home, "epics") == 1
    assert _count(home, "mo_inbox_gates") == 1
    assert _recover_spawns(spawned) == []


def test_a_run_that_is_not_parked_keeps_the_pass_1_stop(home, monkeypatch, spawned):
    """The guard keys on ``waiting_on_fix`` alone — any other state is pass 1.

    A run whose park was cleared (SDE-4 revived it; or the state was rewritten)
    is an ordinary failed run again: the same failure signature must hit the
    no-progress stop rule and be recorded as a ``human`` give-up, exactly as it
    was before the guard existed.
    """
    _stub_diagnosis(monkeypatch, ENGINE_DIAGNOSIS)
    ar.apply(home, RUN, ar.decide(home, RUN))

    repair_path = home / "runs" / RUN / "repair.json"
    repaired = json.loads(repair_path.read_text())
    repaired["state"] = "repairing"  # NOT waiting_on_fix any more
    repair_path.write_text(json.dumps(repaired))

    second = ar.decide(home, RUN)
    assert second["action"] == "human"
    assert second["stop"] is True
    assert "no progress" in second["reason"]

    ar.apply(home, RUN, second)
    assert _count(home, "bug_reports") == 1
    assert _count(home, "epics") == 1
    assert _recover_spawns(spawned) == []


def test_mo_auto_repair_zero_outranks_the_parked_guard(home, monkeypatch, spawned):
    """The off switch is checked first: a parked run reports it verbatim.

    ``MO_AUTO_REPAIR=0`` sits above the parked guard in ``_decide_policy``, so
    the operator's opt-out reason wins over the parked reason, ``maybe_repair``
    never even decides, and the parked record stands untouched either way (the
    ``apply`` short-circuit keys on the run's state, not on the reason).
    """
    _stub_diagnosis(monkeypatch, ENGINE_DIAGNOSIS)
    first = ar.apply(home, RUN, ar.decide(home, RUN))
    assert first["escalated"] is True

    repair_path = home / "runs" / RUN / "repair.json"
    before = repair_path.read_bytes()

    monkeypatch.setenv("MO_AUTO_REPAIR", "0")
    decision = ar.decide(home, RUN)
    assert decision["action"] == "none"
    assert "MO_AUTO_REPAIR=0" in decision["reason"]
    assert "waiting on fix epic" not in decision["reason"]

    assert ar.maybe_repair(home, RUN) is None
    assert ar.apply(home, RUN, decision)["action"] == "none"
    assert repair_path.read_bytes() == before


def test_apply_escalate_keeps_the_parked_state_and_appends_the_attempt(home, monkeypatch,
                                                                       spawned):
    """``apply`` must merge onto the state ``escalate`` just wrote, not clobber it."""
    _stub_diagnosis(monkeypatch, ENGINE_DIAGNOSIS)
    result = ar.apply(home, RUN, ar.decide(home, RUN))

    repair = _read_repair(home)
    assert repair["state"] == "waiting_on_fix"
    assert repair["fix_epic_id"] == result["fix_epic_id"]
    assert repair["fix_bug_id"] == result["fix_bug_id"]
    assert repair["triaged"] is True
    assert [a["action"] for a in repair["attempts"]] == ["escalate"]
    # The attempt carries the decision's signature so the stop rules stay sound.
    assert repair["attempts"][0]["signature"]


# ── the frozen policy's refusals ─────────────────────────────────────────────


def _refused(home, monkeypatch, spawned, reason_fragment: str) -> dict:
    _stub_diagnosis(monkeypatch, ENGINE_DIAGNOSIS)
    decision = ar.decide(home, RUN)
    assert decision["action"] == "escalate"  # decide routes regardless
    result = ar.apply(home, RUN, decision)
    assert result["escalated"] is False
    assert result["action"] == "human"
    assert reason_fragment in result["reason"]
    assert _recover_spawns(spawned) == []

    repair = _read_repair(home)
    attempt = repair["attempts"][-1]
    assert attempt["action"] == "human"
    assert reason_fragment in attempt["reason"]  # the policy reason, verbatim
    # A first-failure refusal is a hand-off, not the loop's own give-up.
    assert repair.get("state") != "gave_up"
    return result


def test_daily_cap_refuses_and_records_human(home, monkeypatch, spawned):
    monkeypatch.setenv("MO_FIX_EPIC_DAILY_CAP", "3")
    for i in range(3):
        _seed_self_driving_epic(home, fingerprint=f"fp-cap-{i}", status="done", bug_id_hint=i)

    _refused(home, monkeypatch, spawned, "daily fix-epic cap reached (3/3)")
    # Nothing new filed for this run.
    assert _count(home, "bug_reports") == 3
    assert _count(home, "epics") == 3
    assert _count(home, "mo_inbox_gates") == 0
    assert _rows(home, "SELECT COUNT(*) FROM run_events WHERE event_type=?",
                 (esc.ESCALATE_EVENT,))[0][0] == 0


def test_breaker_refuses_when_recent_fix_epics_all_failed(home, monkeypatch, spawned):
    for i in range(3):
        _seed_self_driving_epic(home, fingerprint=f"fp-break-{i}", status="failed",
                                bug_id_hint=i, created_at=f"2026-10-1{i}T00:00:00Z")

    _refused(home, monkeypatch, spawned, "fix-epic breaker open")
    assert _count(home, "bug_reports") == 3


def test_signature_failed_before_refuses(home, monkeypatch, spawned):
    # The SAME diagnosis signature, whose earlier fix epic already failed.
    _seed_self_driving_epic(
        home, fingerprint=tf.diagnosis_fingerprint(ENGINE_DIAGNOSIS),
        status="escalated", bug_id_hint=1)

    _refused(home, monkeypatch, spawned, "already failed; not re-proposed")
    assert _count(home, "bug_reports") == 1


def test_cap_env_parsing(home, monkeypatch):
    monkeypatch.setenv("MO_FIX_EPIC_DAILY_CAP", "not-an-int")
    assert esc.gather_inputs(home, RUN, ENGINE_DIAGNOSIS).daily_cap == 3
    monkeypatch.setenv("MO_FIX_EPIC_DAILY_CAP", "-4")
    assert esc.gather_inputs(home, RUN, ENGINE_DIAGNOSIS).daily_cap == 0
    monkeypatch.setenv("MO_FIX_EPIC_DAILY_CAP", "7")
    assert esc.gather_inputs(home, RUN, ENGINE_DIAGNOSIS).daily_cap == 7


def test_breaker_needs_a_full_window_of_failures(home, monkeypatch):
    for i in range(2):  # fewer than N=3
        _seed_self_driving_epic(home, fingerprint=f"fp-short-{i}", status="failed",
                                bug_id_hint=i)
    assert esc.gather_inputs(home, RUN, ENGINE_DIAGNOSIS).collapse_tripped is False


# ── gating: the switch, the layer, the source ────────────────────────────────


def test_mo_auto_escalate_zero_restores_the_pre_step_action(home, monkeypatch, spawned):
    monkeypatch.setenv("MO_AUTO_ESCALATE", "0")
    _stub_diagnosis(monkeypatch, ENGINE_DIAGNOSIS)

    decision = ar.decide(home, RUN)
    assert decision["action"] == "human"  # rule 8: the pre-step hand-off
    assert "escalate" not in decision["action"]

    ar.apply(home, RUN, decision)
    assert _count(home, "bug_reports") == 0
    assert _count(home, "epics") == 0


def test_change_layer_never_escalates(home, monkeypatch, spawned):
    _stub_diagnosis(monkeypatch, _diag(layer="change", stage="verify", node="test_verifier",
                                       routes_to="reverify", source="rule",
                                       summary="the change did not verify"))
    decision = ar.decide(home, RUN)
    assert decision["action"] != "escalate"
    ar.apply(home, RUN, decision)
    assert _count(home, "bug_reports") == 0


def test_engine_diagnosis_not_routing_to_fix_epic_does_not_escalate(home, monkeypatch):
    _stub_diagnosis(monkeypatch, _diag(routes_to="human"))
    assert ar.decide(home, RUN)["action"] != "escalate"


def test_missing_diagnosis_does_not_escalate(home, monkeypatch):
    _stub_diagnosis(monkeypatch, None)
    assert ar.decide(home, RUN)["action"] != "escalate"


def test_escalate_never_raises_on_a_broken_home(tmp_path, monkeypatch):
    """c:0 — a home with no state.db degrades to a refusal, not an exception."""
    broken = tmp_path / "broken"
    (broken / "runs" / RUN).mkdir(parents=True)
    result = esc.escalate(broken, RUN, ENGINE_DIAGNOSIS)
    assert result["escalated"] is False
    assert result["reason"].startswith("escalation error:")


# ── file_diagnosis: attribution without a traceback ─────────────────────────


def test_file_diagnosis_attributes_from_the_diagnosis(home):
    res = tf.file_diagnosis(RUN, ENGINE_DIAGNOSIS, home=str(home),
                            db=str(home / "state.db"), promote=True)

    assert res.blame == "mini_ork"
    assert res.bug_id is not None
    assert res.epic_id is not None
    assert res.signature == tf.diagnosis_fingerprint(ENGINE_DIAGNOSIS)

    row = _rows(home, "SELECT agent_role, severity, confidence, title, description, "
                      "run_id FROM bug_reports WHERE id=?", (res.bug_id,))[0]
    assert row[0] == "self-driving"
    assert row[1] == "high"
    assert row[2] == pytest.approx(0.9)
    assert row[3] == tf.diagnosis_title(ENGINE_DIAGNOSIS)
    assert "engine/deliver/publisher" in row[3] or "[engine/deliver] publisher" in row[3]
    # summary + every evidence item as ``kind ref: excerpt`` — no traceback needed.
    assert ENGINE_DIAGNOSIS["summary"] in row[4]
    assert "finish_reason publish_abstain: the publisher withheld" in row[4]
    assert row[5] == RUN


def test_triage_run_cannot_file_this_run_without_a_traceback(home):
    """Why ``file_diagnosis`` exists: the motivating run has no failing node_end.

    ``triage_run`` re-decides blame from traceback frames, so a delivery-stage
    engine fault (a withheld publish) has nothing for it to attribute — and the
    run is *not* filed. ``file_diagnosis`` takes the attribution from the
    diagnosis instead. ``triage_run`` itself is unchanged.
    """
    res = tf.triage_run(RUN, home=str(home), db=str(home / "state.db"),
                        root=os.environ["MINI_ORK_ROOT"], promote=True)
    assert res.bug_id is None
    assert res.blame == "unknown"
    assert _count(home, "bug_reports") == 0


def test_file_diagnosis_dedupes_the_same_fingerprint(home):
    first = tf.file_diagnosis(RUN, ENGINE_DIAGNOSIS, home=str(home),
                              db=str(home / "state.db"), promote=True)
    second = tf.file_diagnosis(RUN, ENGINE_DIAGNOSIS, home=str(home),
                               db=str(home / "state.db"), promote=True)
    assert first.bug_id == second.bug_id
    assert _count(home, "bug_reports") == 1
    assert _count(home, "epics") == 1


# ── the policy as wired (commit d1606e13) ────────────────────────────────────


def test_model_source_engine_diagnosis_clears_the_bootstrap_bar(home):
    model = _diag(source="model", confidence=0.90, rule_id="RN")
    result = esc.escalate(home, RUN, model)
    assert result["escalated"] is True
    assert "bootstrap bar" in result["reason"]


def test_model_source_below_the_bar_is_refused(home):
    model = _diag(source="model", confidence=0.80, rule_id="RN")
    result = esc.escalate(home, RUN, model)
    assert result["escalated"] is False
    assert "0.80 < bootstrap bar (0/30 labels) 0.85" in result["reason"]
    assert _count(home, "bug_reports") == 0
    assert _count(home, "epics") == 0


def test_uncertifiable_model_diagnosis_is_refused(home):
    """>= MIN_LABELS with no calibrated bar: refused, never waved through.

    SDE-3b supplies the real ``labelled_count``/``calibrated_bar``; until then
    ``gather_inputs`` reports 0/None, so this exercises the policy boundary the
    wiring must keep reachable — ``source``/``confidence`` pass through intact.
    """
    inputs = esc.gather_inputs(home, RUN, _diag(source="model", confidence=0.99))
    assert inputs.source == "model"
    assert inputs.confidence == pytest.approx(0.99)

    decision = should_escalate(dataclasses.replace(
        inputs, labelled_count=30, calibrated_bar=None))
    assert decision.escalate is False
    assert "uncertifiable" in decision.reason


def test_gather_inputs_passes_layer_source_confidence_through(home):
    inputs = esc.gather_inputs(home, RUN, _diag(source="model", confidence=0.42))
    assert (inputs.layer, inputs.source) == ("engine", "model")
    assert inputs.confidence == pytest.approx(0.42)
    assert inputs.calibrated_bar is None
    assert inputs.labelled_count == 0
