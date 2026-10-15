"""``mini_ork.stores.diagnosis_ledger`` + the calibrated escalation bar (SDE-3b2).

Hermetic: a temp sqlite file per test, no network, no LLM, no spawn. The store
half (``record`` / ``set_label`` / ``labelled``) runs against a bare DB path; the
escalation half reuses the hand-rolled schema shape ``test_escalation.py``
established, so ``gather_inputs`` and ``escalate`` see the same world they do in
production.

Two things this file exists to pin down, both of which fail *silently* if
broken:

  * **Row orientation.** ``abstain_gate._ltt_min_confidence`` takes
    ``(confidence, is_wrong)``, while the ``label`` column is 1 = right. A
    :func:`labelled` that forgot to invert would calibrate the bar backwards and
    still return a plausible-looking float — hence the explicit orientation case
    and the "bar sits between the wrong-low and the right-high confidences" case.
  * **First label wins.** A relabel that quietly overwrote would let a later,
    lazier guess rewrite the outcome the calibration was built on.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.recovery import escalation as esc
from mini_ork.recovery.escalation_policy import MIN_LABELS
from mini_ork.stores import diagnosis_ledger as dl

RUN = "run-ledger-0001"

# ``diagnose``'s shape as ``test_escalation.py`` builds it: an engine fault at
# deliver/publisher with no traceback, which is what the loop escalates on.
ENGINE_DIAGNOSIS = {
    "version": 2,
    "run_id": RUN,
    "rule_id": "R1",
    "layer": "engine",
    "stage": "deliver",
    "node": "publisher",
    "source": "rule",
    "confidence": 0.9,
    "routes_to": "fix_epic",
    "summary": "the publisher withheld the publish although levels were proven",
    "computed_at": "2026-10-15T00:00:00Z",
}

_TASK_RUNS_DDL = """
CREATE TABLE task_runs (
  id TEXT PRIMARY KEY, recipe TEXT, status TEXT, task_class TEXT,
  cost_usd REAL DEFAULT 0, kickoff_path TEXT, workflow_version TEXT,
  trace_id TEXT, created_at INTEGER, updated_at INTEGER, ended_at INTEGER, notes TEXT
)
"""

_RUN_EVENTS_DDL = """
CREATE TABLE run_events (
  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_run_id TEXT,
  event_type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')), finish_reason TEXT
)
"""

_BUG_REPORTS_DDL = """
CREATE TABLE bug_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
  run_id TEXT, agent_role TEXT NOT NULL, task_class TEXT, observed_in TEXT,
  title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', suggested_fix TEXT,
  severity TEXT NOT NULL DEFAULT 'medium', confidence REAL NOT NULL DEFAULT 0.5,
  frequency INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'open',
  promoted_to_epic_id TEXT, first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
)
"""

_EPICS_DDL = """
CREATE TABLE epics (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
  kickoff_path TEXT, notes TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at TEXT
)
"""

_MO_INBOX_DDL = """
CREATE TABLE mo_inbox_gates (
  inbox_id INTEGER PRIMARY KEY AUTOINCREMENT, gate_id TEXT NOT NULL,
  feature TEXT NOT NULL, phase TEXT, context_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', review_note TEXT,
  enqueued_at INTEGER NOT NULL, resolved_at INTEGER, blocks_dispatch_for TEXT
)
"""

_ALL_DDL = (_TASK_RUNS_DDL, _RUN_EVENTS_DDL, _BUG_REPORTS_DDL, _EPICS_DDL, _MO_INBOX_DDL)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A bare DB path: no schema at all — the store must heal it itself."""
    return tmp_path / "ledger.db"


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A temp mini-ork home with the schema and this run's directory."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": "chapter-review"}))

    con = sqlite3.connect(home / "state.db")
    for ddl in _ALL_DDL:
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
        ("MINI_ORK_DB", str(home / "state.db")),
        ("MINI_ORK_ROOT", str(root)),
    ):
        monkeypatch.setenv(key, value)
    for key in ("MO_FIX_EPIC_DAILY_CAP", "MO_FIX_EPIC_BREAKER_N", "MO_AUTO_ESCALATE"):
        monkeypatch.delenv(key, raising=False)
    return home


def _diag(**over) -> dict:
    return {**ENGINE_DIAGNOSIS, **over}


def _rows(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _ledger_rows(home: Path) -> list[dict]:
    con = sqlite3.connect(home / "state.db")
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM diagnosis_ledger ORDER BY id").fetchall()]
    finally:
        con.close()


def _seed_labelled_models(db: Path, *, right: int, conf_right: float,
                          wrong: int, conf_wrong: float) -> None:
    """Record + label ``right``+``wrong`` model diagnoses at fixed confidences."""
    n = 0
    for correct, count, conf in ((True, right, conf_right), (False, wrong, conf_wrong)):
        for _ in range(count):
            n += 1
            diag = _diag(rule_id=f"RN{n}", source="model", confidence=conf,
                         summary=f"synthetic model diagnosis {n}")
            ledger_id = dl.record(db, f"run-seed-{n}", diag, action="escalate")
            assert dl.set_label(db, ledger_id, correct, "synthetic") is True


# ── record: idempotency ──────────────────────────────────────────────────────


def test_record_twice_with_the_same_key_is_one_row_with_the_same_id(db):
    first = dl.record(db, RUN, ENGINE_DIAGNOSIS, action="escalate", fix_epic_id="ep-1")
    second = dl.record(db, RUN, ENGINE_DIAGNOSIS, action="escalate", fix_epic_id="ep-1")

    assert first == second
    rows = _rows(db, "SELECT id, run_id, source, action, fix_epic_id FROM diagnosis_ledger")
    assert len(rows) == 1
    assert rows[0][1:] == (RUN, "rule", "escalate", "ep-1")


def test_record_heals_a_db_that_never_ran_the_migration(db):
    """No migration, no schema: the store creates the ledger itself."""
    dl.record(db, RUN, ENGINE_DIAGNOSIS, action="escalate")
    assert len(_rows(db, "SELECT id FROM diagnosis_ledger")) == 1


def test_record_derives_a_distinct_signature_per_diagnosis(db):
    dl.record(db, RUN, ENGINE_DIAGNOSIS, action="escalate")
    dl.record(db, RUN, _diag(rule_id="R2"), action="escalate")
    assert len(_rows(db, "SELECT id FROM diagnosis_ledger")) == 2


def test_record_honours_an_explicit_signature(db):
    """A caller that knows the signature keeps it — the key is not re-derived."""
    dl.record(db, RUN, _diag(signature="sig-abc"), action="escalate")
    dl.record(db, RUN, _diag(signature="sig-abc", summary="a different summary"),
              action="escalate")
    rows = _rows(db, "SELECT signature FROM diagnosis_ledger")
    assert rows == [("sig-abc",)]


def test_record_fills_the_audit_columns(db):
    dl.record(db, RUN, _diag(source="model", confidence=0.62, model_lane="kimi_lens"),
              action="human")
    row = _rows(db, "SELECT source, layer, routes_to, confidence, model_lane, action, "
                    "created_at FROM diagnosis_ledger")[0]
    assert row[0] == "model"
    assert row[1] == "engine"
    assert row[2] == "fix_epic"
    assert row[3] == pytest.approx(0.62)
    assert row[4] == "kimi_lens"
    assert row[5] == "human"
    assert row[6] == pytest.approx(int(time.time()), abs=5)


def test_record_stores_a_missing_confidence_as_null(db):
    """Not 0.0: "stated no confidence" must not read as "certainly wrong"."""
    dl.record(db, RUN, {k: v for k, v in ENGINE_DIAGNOSIS.items() if k != "confidence"},
              action="escalate")
    assert _rows(db, "SELECT confidence FROM diagnosis_ledger") == [(None,)]


# ── set_label: first label wins ──────────────────────────────────────────────


def test_set_label_refuses_to_relabel(db):
    ledger_id = dl.record(db, RUN, ENGINE_DIAGNOSIS, action="escalate")
    assert dl.set_label(db, ledger_id, True, "the fix landed and the run passed") is True
    # A second opinion must not rewrite the outcome the calibration is built on.
    assert dl.set_label(db, ledger_id, False, "on reflection, wrong") is False

    row = _rows(db, "SELECT label, label_reason FROM diagnosis_ledger WHERE id=?",
                (ledger_id,))[0]
    assert row[0] == 1
    assert row[1] == "the fix landed and the run passed"


def test_set_label_on_a_missing_row_is_a_no_op(db):
    dl.record(db, RUN, ENGINE_DIAGNOSIS, action="escalate")
    assert dl.set_label(db, 9999, True, "no such row") is False
    assert _rows(db, "SELECT label FROM diagnosis_ledger") == [(None,)]


# ── labelled: the LTT input ──────────────────────────────────────────────────


def test_labelled_inverts_the_label_into_is_wrong(db):
    """The orientation the LTT helper expects: (confidence, is_wrong)."""
    right = dl.record(db, RUN, _diag(source="model", confidence=0.9), action="escalate")
    wrong = dl.record(db, RUN, _diag(rule_id="R2", source="model", confidence=0.4),
                      action="escalate")
    dl.set_label(db, right, True, "right")
    dl.set_label(db, wrong, False, "wrong")

    assert sorted(dl.labelled(db, "model")) == [(0.4, True), (0.9, False)]


def test_labelled_filters_by_source(db):
    m = dl.record(db, RUN, _diag(source="model", confidence=0.9), action="escalate")
    r = dl.record(db, RUN, _diag(rule_id="R2", source="rule", confidence=0.9),
                  action="escalate")
    dl.set_label(db, m, True, "")
    dl.set_label(db, r, True, "")

    assert dl.labelled(db, "model") == [(0.9, False)]
    assert dl.labelled(db, "rule") == [(0.9, False)]


def test_labelled_skips_unlabelled_and_confidence_less_rows(db):
    unlabelled = dl.record(db, RUN, _diag(source="model", confidence=0.9),
                           action="escalate")
    assert unlabelled  # recorded, but never labelled
    labelled = dl.record(db, RUN, _diag(rule_id="R2", source="model", confidence=0.9),
                         action="escalate")
    dl.set_label(db, labelled, True, "")
    # A labelled row with no confidence cannot calibrate a confidence threshold.
    no_conf = dl.record(db, RUN,
                        {k: v for k, v in _diag(rule_id="R3", source="model").items()
                         if k != "confidence"},
                        action="escalate")
    dl.set_label(db, no_conf, False, "")

    assert dl.labelled(db, "model") == [(0.9, False)]


def test_labelled_on_a_home_with_no_ledger_is_empty_not_an_error(tmp_path):
    """The pre-0073 home: gather_inputs must read 0 labels, not blow up."""
    assert dl.labelled(tmp_path / "state.db", "model") == []


# ── gather_inputs: the bar the policy reads ──────────────────────────────────


def test_below_min_labels_the_bar_stays_none(home):
    _seed_labelled_models(home / "state.db", right=MIN_LABELS - 1, conf_right=0.99,
                          wrong=0, conf_wrong=0.0)
    inputs = esc.gather_inputs(home, RUN, _diag(source="model", confidence=0.99))

    assert inputs.labelled_count == MIN_LABELS - 1
    assert inputs.calibrated_bar is None  # the frozen policy's bootstrap bar applies


def test_calibrated_bar_sits_between_the_wrong_and_the_right_confidences(home):
    """40 labels, the wrong ones at low confidence → a bar between the two modes."""
    _seed_labelled_models(home / "state.db", right=30, conf_right=0.90,
                          wrong=10, conf_wrong=0.30)
    inputs = esc.gather_inputs(home, RUN, _diag(source="model", confidence=0.90))

    assert inputs.labelled_count == 40 >= MIN_LABELS
    assert inputs.calibrated_bar is not None
    assert 0.30 < inputs.calibrated_bar <= 0.90


def test_an_uncertifiable_diagnoser_yields_no_bar_and_the_policy_refuses(home):
    """40 labels whose high-confidence rows are wrong: nothing controls the risk.

    The bar must collapse to ``None`` (not to some permissive number) and the
    frozen policy must then refuse the model diagnosis outright — never wave it
    through on the bootstrap bar it has outgrown.
    """
    _seed_labelled_models(home / "state.db", right=20, conf_right=0.90,
                          wrong=20, conf_wrong=0.99)
    model = _diag(source="model", confidence=0.99)
    inputs = esc.gather_inputs(home, RUN, model)

    assert inputs.labelled_count == 40
    assert inputs.calibrated_bar is None

    result = esc.escalate(home, RUN, model)
    assert result["escalated"] is False
    assert "uncertifiable" in result["reason"]


def test_a_calibrated_bar_admits_a_confident_model_diagnosis(home):
    """The crossover: past MIN_LABELS the calibrated bar replaces the bootstrap.

    With the wrong rows at 0.30 the bar lands at 0.90, so a 0.95 diagnosis clears
    it while a 0.50 one does not — and the reason names the *calibrated* bar, not
    the bootstrap one.
    """
    home_db = home / "state.db"
    _seed_labelled_models(home_db, right=30, conf_right=0.90, wrong=10, conf_wrong=0.30)

    cleared = esc.gather_inputs(home, RUN, _diag(source="model", confidence=0.95))
    # The smallest threshold whose (k+1)/(n+1) bound clears alpha: at 0.90 the
    # accepted set is the 30 right rows with 0 wrong → 1/31; at 0.30 the 10 wrong
    # rows are admitted too → 11/41. So 0.90 is the only certifiable candidate.
    assert cleared.calibrated_bar == pytest.approx(0.90)

    admitted = esc.escalate(home, RUN, _diag(source="model", confidence=0.95))
    assert admitted["escalated"] is True
    assert "calibrated bar" in admitted["reason"]

    refused = esc.escalate(home, RUN, _diag(source="model", confidence=0.50,
                                            rule_id="R9"))
    assert refused["escalated"] is False
    assert "calibrated bar" in refused["reason"]


def test_gather_inputs_still_passes_layer_source_confidence_through(home):
    inputs = esc.gather_inputs(home, RUN, _diag(source="model", confidence=0.42))
    assert (inputs.layer, inputs.source) == ("engine", "model")
    assert inputs.confidence == pytest.approx(0.42)
    assert inputs.labelled_count == 0


# ── escalate: recording what the loop acted on ───────────────────────────────


def test_escalate_records_one_ledger_row_per_run_with_the_fix_epic(home):
    first = esc.escalate(home, RUN, ENGINE_DIAGNOSIS)
    second = esc.escalate(home, RUN, ENGINE_DIAGNOSIS)

    assert first["escalated"] is True and second["escalated"] is True
    assert first["fix_epic_id"] == second["fix_epic_id"]

    rows = _ledger_rows(home)
    assert len(rows) == 1  # the dedupe key held: a re-escalate is not a new row
    row = rows[0]
    assert row["run_id"] == RUN
    assert row["source"] == "rule"
    assert row["layer"] == "engine"
    assert row["routes_to"] == "fix_epic"
    assert row["confidence"] == pytest.approx(0.9)
    assert row["action"] == "escalate"
    assert row["fix_epic_id"] == first["fix_epic_id"]
    assert row["label"] is None  # the outcome is not known yet — SDE-4 labels it


def test_a_refused_escalation_is_recorded_as_human(home, monkeypatch):
    """Refusals are outcomes too — SDE-4 sees both halves of the diagnoser's record.

    ``MO_FIX_EPIC_DAILY_CAP=0`` refuses every escalation, so the loop acts on the
    diagnosis by handing the run to a human. The ledger must hold that, with no
    fix epic attached, and the ledger table must be created on a home that never
    ran migration 0073 (this fixture's schema has no ``diagnosis_ledger``).
    """
    monkeypatch.setenv("MO_FIX_EPIC_DAILY_CAP", "0")
    result = esc.escalate(home, RUN, ENGINE_DIAGNOSIS)

    assert result["escalated"] is False
    assert "daily fix-epic cap reached (0/0)" in result["reason"]

    rows = _ledger_rows(home)
    assert len(rows) == 1
    assert rows[0]["action"] == "human"
    assert rows[0]["fix_epic_id"] is None


def test_a_broken_ledger_never_blocks_an_escalation(home, monkeypatch):
    """A ledger that raises must cost the run nothing — not even a warning.

    The escalation itself has already succeeded when the ledger is written, so a
    ledger failure that escaped here would turn a fix epic into an
    ``escalation error`` and the run would lose the fix it just filed.
    """
    def _boom(*a, **kw):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(dl, "record", _boom)
    result = esc.escalate(home, RUN, ENGINE_DIAGNOSIS)

    assert result["escalated"] is True
    assert result["fix_epic_id"]
    # Nothing was written — not even the table the writer would have created.
    assert _rows(
        home / "state.db",
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='diagnosis_ledger'",
    )[0][0] == 0


def test_a_broken_ledger_never_breaks_a_refusal_either(home, monkeypatch):
    monkeypatch.setenv("MO_FIX_EPIC_DAILY_CAP", "0")

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(dl, "record", _boom)
    result = esc.escalate(home, RUN, ENGINE_DIAGNOSIS)
    assert result["escalated"] is False
    assert "daily fix-epic cap" in result["reason"]


def test_escalate_never_raises_on_a_broken_home(tmp_path):
    """c:0 still holds with the ledger in the path."""
    broken = tmp_path / "broken"
    (broken / "runs" / RUN).mkdir(parents=True)
    result = esc.escalate(broken, RUN, ENGINE_DIAGNOSIS)
    assert result["escalated"] is False
    assert result["reason"].startswith("escalation error:")


# ── the migration and the runtime DDL agree ──────────────────────────────────


def test_the_migration_and_the_store_schema_agree(db):
    """A home healed at runtime must hold every column the migration declares.

    The store re-runs its own DDL (``_ensure_table``) so a pre-0073 home works.
    If the two texts drift, a migrated deployment and a healed one answer
    different questions from the same table.
    """
    migration = (Path(__file__).resolve().parents[2]
                 / "db" / "migrations" / "0073_diagnosis_ledger.sql").read_text()
    dl.record(db, RUN, ENGINE_DIAGNOSIS, action="escalate")

    con = sqlite3.connect(db)
    try:
        columns = {r[1] for r in con.execute("PRAGMA table_info(diagnosis_ledger)")}
        indexes = {r[1] for r in con.execute("PRAGMA index_list(diagnosis_ledger)")}
    finally:
        con.close()

    for column in columns:
        assert f"{column} " in migration, f"{column} is not declared by 0073"
    # The dedupe key itself, in both texts.
    assert "idx_diagnosis_ledger_unique" in migration
    assert "idx_diagnosis_ledger_unique" in indexes
