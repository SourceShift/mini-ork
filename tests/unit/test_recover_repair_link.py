"""Recover → ``verifier_results.repair_run_id`` wire (``db:V3``).

``verifier_chain_repair`` existed but had no production caller, so every
``verifier_results`` row landed with ``repair_run_id`` NULL and a repair that
answered a run's failures was never recorded. ``planner.cli_main`` now links
the resumed run's unlinked ``verdict='fail'`` rows to the run it just
dispatched — but only when auto-repair spawned the recover (the captured
``MO_AUTO_REPAIR_RUN_ID`` marker). The directions pinned here are the kickoff's:

  * an auto-repair-marked recover links every unlinked ``fail`` row of the
    resumed run, and only those;
  * a marker-less (human) recover links nothing;
  * a refused or undispatched recover links nothing — no repair served the
    failure, so no row may claim one;
  * the id written is the resumed run's own id, never the marker's per-attempt
    child id;
  * a DB error is logged to stderr and never changes the recover's exit code;
  * pass/indeterminate/vacuous rows, other runs' rows, an already-linked row
    and every annotation column stay byte-identical.

The last test is G8's measurement hook: ``mini_ork.rsi.cycle._exercise_once``
runs this file against a *copy* of the measured DB (exported as
``MINI_ORK_DB``) and re-asks ``db:V3`` on that copy. A private ``tmp_path`` DB
would leave the rule red, so when G8 supplies its copy the producer is driven
against it and the link lands where the rule is measured.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_ork.recovery import planner

# G8's ``_exercise_once`` exports the exercised copy as ``MINI_ORK_DB``.
# ``tests/conftest.py`` pops it before every test, so it is only visible at
# import time; accept it only via G8's ``exercise-{base,post}.db`` shape so an
# inherited operator DB can never be written.
_G8_DB = os.environ.get("MINI_ORK_DB") or ""

RUN_ID = "run-1792000000-4242"
OTHER_RUN = "run-1791999999-1111"
# Deliberately different from RUN_ID: the marker names the spawning repair,
# the row must record the run the recover actually resumed.
MARKER_CHILD_ID = "repair-child-777"
EARLIER_REPAIR = "repair-earlier-1"

WORKFLOW_YAML = """\
nodes:
  - name: planner
    type: planner
  - name: implementer
    type: implementer
edges:
  - from: planner
    to: implementer
    edge_type: depends_on
"""

_TASK_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY,
  task_class TEXT NOT NULL,
  recipe TEXT,
  workflow_version TEXT NOT NULL DEFAULT 'latest',
  kickoff_path TEXT NOT NULL,
  plan_path TEXT,
  plan_hash TEXT,
  artifact_path TEXT,
  artifact_hash TEXT,
  status TEXT NOT NULL DEFAULT 'classified',
  verdict TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  trace_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  ended_at INTEGER,
  parent_epic_id TEXT,
  notes TEXT
);
"""

_DDL = _TASK_RUNS_DDL + """
CREATE TABLE IF NOT EXISTS node_checkpoints (
  run_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  recipe_version TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  artifact_manifest_json TEXT NOT NULL,
  session_ref TEXT,
  failure_class TEXT,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_attempts (
  id TEXT, run_id TEXT, node_id TEXT, result TEXT,
  failure_class TEXT, started_at INTEGER, ended_at INTEGER
);
CREATE TABLE IF NOT EXISTS remote_procs (
  id TEXT, run_id TEXT, node_id TEXT, state TEXT, consumed INTEGER
);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY, run_id TEXT, event_type TEXT,
  payload_json TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS verifier_results (
    result_id            TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    verifier_name        TEXT NOT NULL,
    rubric_id            TEXT,
    verdict              TEXT NOT NULL,
    confidence           REAL,
    scored_axes_json     TEXT,
    is_false_positive    INTEGER NOT NULL DEFAULT 0,
    is_false_negative    INTEGER NOT NULL DEFAULT 0,
    annotated_by         TEXT,
    annotated_at         INTEGER,
    repair_run_id        TEXT,
    notes                TEXT,
    created_at           INTEGER NOT NULL DEFAULT 0
);
"""

# (result_id, run_id, verdict, repair_run_id, fp, fn, annotated_by, annotated_at, notes)
_ROWS = [
    ("vr-fail-open-1", RUN_ID, "fail", None, 1, 0, "ops@example", 1700000000, "human call"),
    ("vr-fail-open-2", RUN_ID, "fail", None, 0, 0, None, None, None),
    ("vr-fail-linked", RUN_ID, "fail", EARLIER_REPAIR, 0, 0, None, None, None),
    ("vr-pass", RUN_ID, "pass", None, 0, 0, None, None, None),
    ("vr-indet", RUN_ID, "indeterminate", None, 0, 0, None, None, None),
    ("vr-vacuous", RUN_ID, "vacuous", None, 0, 0, None, None, None),
    ("vr-other-run", OTHER_RUN, "fail", None, 0, 0, None, None, None),
]

# Keys the recover flow mutates in the process env; scrubbed so a leaked value
# from the operator's shell (or a sibling test) cannot steer the drive.
_SCRUB_ENV = (
    "MINI_ORK_TASK_CLASS", "MINI_ORK_RECIPE", "MINI_ORK_AGENTS",
    "MO_PLACEMENT", "MO_NODE_ENV",
    "MO_AUTO_REPAIR_ATTEMPT", "MO_AUTO_REPAIR_RUN_ID", "MO_AUTO_REPAIR_WAIT_PID",
    "MINI_ORK_PLAN_PATH", "MO_INTERACTIVE", "MO_TARGET_CWD",
    "MINI_ORK_RECOVERY_RUN_ID", "MINI_ORK_RECOVERY_SKU",
    "MINI_ORK_RECOVERY_CLOSURE", "MINI_ORK_RECOVERY_STRATEGY",
    "MINI_ORK_RECOVERY_FROM", "MINI_ORK_RECOVERY_REQUEST",
    "MINI_ORK_LEASE_TOKEN", "MINI_ORK_LEASE_RUN_ID", "MINI_ORK_ROOT",
)


def _insert_rows(db: Path, rows, *, run_id: str = RUN_ID) -> None:
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT OR REPLACE INTO task_runs (id, task_class, recipe,"
            " kickoff_path, status, cost_usd, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "spec", "spec-recipe", "", "executing", 0.0,
             1700000000, 1700000000))
        for row in rows:
            con.execute(
                "INSERT OR REPLACE INTO verifier_results (result_id, run_id,"
                " verifier_name, verdict, repair_run_id, is_false_positive,"
                " is_false_negative, annotated_by, annotated_at, notes,"
                " created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row[0], row[1], "static-check", row[2], row[3], row[4],
                 row[5], row[6], row[7], row[8], 1700000000))
        con.commit()
    finally:
        con.close()


def _build_run_dir(home: Path, run_id: str, target: Path) -> Path:
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(
        json.dumps({"objective": "repair-link-fixture"}), encoding="utf-8")
    (run_dir / "run_profile.json").write_text(
        json.dumps({"recipe": "spec-recipe", "task_class": "spec",
                    "roots": {"target": str(target)}}),
        encoding="utf-8")
    return run_dir


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A hermetic home + run dir + state.db that drives a dispatched recover."""
    home = tmp_path / "home"
    run_dir = _build_run_dir(home, RUN_ID, tmp_path / "target")
    (tmp_path / "target").mkdir(exist_ok=True)
    db = home / "state.db"
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(WORKFLOW_YAML, encoding="utf-8")
    con = sqlite3.connect(str(db))
    con.executescript(_DDL)
    con.close()
    _insert_rows(db, _ROWS)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(workflow))
    monkeypatch.setenv("MINI_ORK_TASK_CLASS", "spec")
    # Keep the teardown hooks quiet and process-local: neither var is one of
    # the captured repair markers, so neither can mask the hook under test.
    monkeypatch.setenv("MO_AUTO_REPAIR", "0")
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    for key in _SCRUB_ENV:
        if key != "MINI_ORK_TASK_CLASS":
            monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(home=home, run_dir=run_dir, db=db,
                           workflow=workflow, target=tmp_path / "target")


@contextmanager
def _env_snapshot():
    """Undo every env mutation the recover flow makes (it writes os.environ)."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _results(db: Path) -> dict:
    """All verifier_results rows keyed by result_id, as plain dicts."""
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return {
            r["result_id"]: dict(r)
            for r in con.execute("SELECT * FROM verifier_results").fetchall()
        }
    finally:
        con.close()


def _drive(argv, *, marker=None, env=None, execute_rc=0):
    """Run ``planner.cli_main`` with a recording executor stub.

    Returns (rc, calls) where calls is the list of argvs the stub received —
    non-empty proves a closure was genuinely dispatched (the success path).
    """
    calls: list[list[str]] = []

    def stub(exec_argv):
        calls.append(list(exec_argv))
        return execute_rc

    with _env_snapshot():
        if marker is not None:
            os.environ["MO_AUTO_REPAIR_RUN_ID"] = marker
        for key, value in (env or {}).items():
            os.environ[key] = value
        rc = planner.cli_main(list(argv), execute_fn=stub)
    return rc, calls


# ── hermetic probes ──────────────────────────────────────────────────────────


def test_auto_repair_marked_recover_links_unlinked_fail_rows(rig):
    """Only the resumed run's unlinked ``fail`` rows gain a repair link."""
    rc, calls = _drive([RUN_ID], marker=RUN_ID)
    assert rc == 0, "a dispatched recover exits 0"
    assert calls, "the closure must actually reach the executor (success path)"
    rows = _results(rig.db)
    assert rows["vr-fail-open-1"]["repair_run_id"] == RUN_ID
    assert rows["vr-fail-open-2"]["repair_run_id"] == RUN_ID, (
        "every unlinked fail row — not just the first — must be linked")


def test_markerless_human_recover_links_nothing(rig):
    """A human recover served no repair, so no row may claim one."""
    rc, calls = _drive([RUN_ID], marker=None)
    assert rc == 0
    assert calls, "the human recover genuinely dispatched a closure"
    rows = _results(rig.db)
    assert rows["vr-fail-open-1"]["repair_run_id"] is None
    assert rows["vr-fail-open-2"]["repair_run_id"] is None


def test_refused_recover_links_nothing(rig):
    """A recover that refuses before dispatch writes no calibration claim."""
    rc, calls = _drive(
        [RUN_ID], marker=RUN_ID,
        env={"MINI_ORK_RUN_DIR": str(rig.home / "runs" / "no-such-run")})
    assert rc != 0, "a recover whose run dir is missing must refuse (non-zero rc)"
    assert not calls, "a refused recover never reaches the executor"
    rows = _results(rig.db)
    assert rows["vr-fail-open-1"]["repair_run_id"] is None
    assert rows["vr-fail-open-2"]["repair_run_id"] is None


def test_undispatched_recover_links_nothing(rig):
    """``--strategy pause`` dispatches nothing, so it links nothing."""
    rc, calls = _drive([RUN_ID, "--strategy", "pause"], marker=RUN_ID)
    assert rc == 0, "--strategy pause returns 0 without dispatching"
    assert not calls, "--strategy pause computes the plan and stops"
    rows = _results(rig.db)
    assert rows["vr-fail-open-1"]["repair_run_id"] is None
    assert rows["vr-fail-open-2"]["repair_run_id"] is None


def test_written_id_is_the_resumed_run_not_the_marker(rig):
    """The marker names the spawning repair; the row records the resumed run."""
    rc, calls = _drive([RUN_ID], marker=MARKER_CHILD_ID)
    assert rc == 0
    assert calls
    rows = _results(rig.db)
    assert rows["vr-fail-open-1"]["repair_run_id"] == RUN_ID
    assert rows["vr-fail-open-2"]["repair_run_id"] == RUN_ID


def test_db_error_is_logged_and_never_fails_the_dispatch(rig, capsys):
    """A calibration write error may never fail the repair it describes."""
    con = sqlite3.connect(str(rig.db))
    con.execute("DROP TABLE verifier_results")
    con.commit()
    con.close()
    rc, calls = _drive([RUN_ID], marker=RUN_ID)
    assert calls, "the dispatch must complete despite the calibration error"
    assert rc == 0, "the recover's exit code must be the executor's, not the hook's"
    assert capsys.readouterr().err.strip() != "", (
        "the swallowed DB error must be logged to stderr, not silenced")


def test_linking_leaves_everything_else_untouched(rig):
    """Only ``repair_run_id`` changes, and only on the targeted rows."""
    before = _results(rig.db)
    rc, calls = _drive([RUN_ID], marker=RUN_ID)
    assert rc == 0
    assert calls
    after = _results(rig.db)
    for result_id, row in before.items():
        expected = dict(row)
        if result_id in ("vr-fail-open-1", "vr-fail-open-2"):
            expected["repair_run_id"] = RUN_ID
        assert after[result_id] == expected, (
            f"{result_id}: only repair_run_id may change on the resumed run's"
            " unlinked fail rows — verdict, annotations, notes, other runs'"
            " rows and the already-linked repair must be byte-identical")


# ── G8 measurement hook ──────────────────────────────────────────────────────


def _is_exercise_copy(db: str) -> bool:
    """True only for G8's ``_exercise_once`` copy of the measured DB.

    Its shape (``mini_ork/rsi/cycle.py``) is
    ``<home>/rsi/<cycle_id>/exercise-{base,post}.db`` beside an
    ``exercise-{base,post}-home`` dir. A node lane also exports
    ``MINI_ORK_DB`` pointing at the operator's live ``state.db``; the shape
    check keeps this file from writing there.
    """
    path = Path(db)
    return (path.name.startswith("exercise-") and path.suffix == ".db"
            and path.parent.parent.name == "rsi"
            and (path.parent / f"{path.stem}-home").is_dir())


_G8_RUN = "rsi-g8-repair-link"
_G8_ROWS = [
    ("g8-repair-fail-1", _G8_RUN, "fail", None, 1, 0, "ops@example", 1700000000, None),
    ("g8-repair-fail-2", _G8_RUN, "fail", None, 0, 0, None, None, None),
    ("g8-repair-pass", _G8_RUN, "pass", None, 0, 0, None, None, None),
]


def test_g8_exercised_copy_records_a_repair_link(tmp_path, monkeypatch):
    """``db:V3`` closes only when the *producer* writes a repair link.

    G8 (``rsi.cycle._exercise_once``) re-asks ``db:V3`` after running this
    file against a copy of the measured DB; the rule's success metric is "at
    least one labelled result or repair link exists". Writing the column from
    a fixture would fake that measurement, so this drives the same
    ``cli_main`` producer an auto-repair-spawned recover takes, against the
    exercise copy. It skips cleanly unless ``MINI_ORK_DB`` is G8's copy, so
    the ordinary suite stays hermetic and never touches a live state DB.
    """
    db = _G8_DB
    if not _is_exercise_copy(db):
        pytest.skip("G8 did not supply an exercised measured-corpus DB")

    home = tmp_path / "home"
    run_dir = _build_run_dir(home, _G8_RUN, tmp_path / "target")
    (tmp_path / "target").mkdir(exist_ok=True)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(WORKFLOW_YAML, encoding="utf-8")
    # The hint gate resolves the run status from ``MINI_ORK_HOME/state.db``;
    # an ``executing`` row there short-circuits ``load_or_compute`` so no hint
    # can refuse this drive. The hook still writes through ``--db`` below.
    hint_home_db = home / "state.db"
    con = sqlite3.connect(str(hint_home_db))
    con.executescript(_TASK_RUNS_DDL)
    con.execute(
        "INSERT OR REPLACE INTO task_runs (id, task_class, kickoff_path,"
        " status, cost_usd, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_G8_RUN, "spec", "", "executing", 0.0, 1700000000, 1700000000))
    con.commit()
    con.close()
    _insert_rows(Path(db), _G8_ROWS, run_id=_G8_RUN)

    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_TASK_CLASS", "spec")
    monkeypatch.setenv("MO_AUTO_REPAIR", "0")
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")
    for key in _SCRUB_ENV:
        if key != "MINI_ORK_TASK_CLASS":
            monkeypatch.delenv(key, raising=False)

    rc, calls = _drive([_G8_RUN, "--db", db, "--workflow", str(workflow)],
                       marker=_G8_RUN)
    assert rc == 0
    assert calls, "the exercise drive must reach the executor"

    con = sqlite3.connect(db)
    try:
        linked = con.execute(
            "SELECT count(*) FROM verifier_results"
            " WHERE run_id=? AND verdict='fail' AND repair_run_id IS NOT NULL",
            (_G8_RUN,)).fetchone()[0]
    finally:
        con.close()
    assert linked == 2, (
        "the exercised copy must carry a producer-written repair link so "
        "db:V3 ('at least one labelled result or repair link exists') closes")
