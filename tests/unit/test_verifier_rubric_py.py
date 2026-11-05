"""Standalone unit tests for ``mini_ork.gates.verifier_rubric``.

Replaces the bash-parity gate (against ``lib/verifier_rubric.sh``) as part
of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer invokes the LIVE bash subprocess
— it asserts the port's behaviour directly. The expected values below are
the semantic contract the bash side used to pin (UPSERT row shape,
rubric_get hit/miss output, result_id format, annotate/chain-repair
writes, fp_rate math + .4f formatting), now asserted on the port's
output.

Cases:

  (a) rubric_register               — UPSERT row matches the registered
                                       fields (updated_at ignored).
  (b) rubric_get hit                — JSON dict matches the registered row.
  (c) rubric_get miss               — literal 'null'.
  (d) verifier_result_record        — inserted row matches the recorded
                                       fields; result_id matches
                                       ^vr-[0-9a-f]{12}$.
  (e) verifier_result_annotate fp   — is_false_positive=1,
                                       is_false_negative=0, annotated_by
                                       set, annotated_at non-null.
  (f) verifier_chain_repair         — repair_run_id set; nothing else
                                       changes.
  (g) verifier_fp_rate math         — 4 results + 1 fp → '0.2500';
                                       reverse-order sub-case proves
                                       order-independence (same 0.25);
                                       empty case → '0.0'.
  (h) rubric_criteria_register      — one verifier_criteria row per scored
                                       item keyed by (rubric_id, axis_name);
                                       re-registering the same items is
                                       idempotent and mints no new ids.
  (i) G8 exercised copy             — drives the production recorder against
                                       the DB copy ``rsi.cycle._exercise_once``
                                       exports as ``MINI_ORK_DB``, so ``db:V2``
                                       is measured against rows the producer
                                       actually wrote (skipped elsewhere).

Tolerance: floats 1e-6. The fp_rate ``.4f`` precision is bounded at
5e-5; 1e-6 is a strict superset.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import verifier_rubric as vr  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

# Float tolerance (fp_rate is .4f so precision is bounded at 5e-5;
# 1e-6 is a strict superset).
_FLOAT_TOL = 1e-6

# result_id format: vr-<secrets.token_hex(6)> → 12 lowercase hex chars.
_RESULT_ID_RE = re.compile(r"^vr-[0-9a-f]{12}$")

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


# ─────────────────────────────────────────────────────────────────────────────
# DB scaffold fixture (init_db against tmp_path)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path):
    """Spin up a real mini-ork SQLite DB via init_db with a unique path
    per test. Migration 0025_verifier_rubrics.sql applies inside init_db;
    rubric + results tables exist for the test."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    if rc != 0:
        pytest.skip(f"init_db failed:\n{out}\n{err}")
    return dbp


# ─────────────────────────────────────────────────────────────────────────────
# Columns we compare in DB rows — write-timestamps differ per call
# ─────────────────────────────────────────────────────────────────────────────
_RUBRIC_ROW_COLS = (
    "rubric_id", "name", "task_class", "axes_json",
    "is_active",
)
_RESULT_ROW_COLS = (
    "run_id", "verifier_name", "rubric_id", "verdict",
    "confidence", "scored_axes_json",
    "is_false_positive", "is_false_negative",
    "annotated_by", "annotated_at",
    "repair_run_id", "notes",
)


def _select_result_row(db_path: str, result_id: str) -> dict:
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT " + ",".join(_RESULT_ROW_COLS)
            + " FROM verifier_results WHERE result_id=?",
            (result_id,),
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) rubric_register — UPSERT row matches the registered fields
# ─────────────────────────────────────────────────────────────────────────────
def test_rubric_register(temp_db):
    """Register rubric_id='r1'; the row's logical columns match the
    registered values. A second register with the same id UPSERTs — there
    is exactly ONE row. updated_at is ignored (write-timestamp)."""
    axes = '{"axes":["clarity","scope"]}'

    vr.rubric_register(
        temp_db,
        rubric_id="r1",
        name="Test rubric",
        task_class="framework_edit",
        axes_json=axes,
    )
    # UPSERT semantics: a second register overwrites the same row.
    vr.rubric_register(
        temp_db,
        rubric_id="r1",
        name="Test rubric",
        task_class="framework_edit",
        axes_json=axes,
    )

    con = sqlite3.connect(temp_db)
    try:
        rows = con.execute(
            "SELECT " + ",".join(_RUBRIC_ROW_COLS)
            + " FROM verifier_rubrics WHERE rubric_id=?",
            ("r1",),
        ).fetchall()
    finally:
        con.close()

    assert len(rows) == 1, (
        f"expected 1 row after UPSERT (same rubric_id), got {len(rows)}: {rows}"
    )
    r = rows[0]
    expected = ("r1", "Test rubric", "framework_edit", axes, 1)
    assert r == expected, (
        f"rubric row fields don't match expected:\nrow     ={r}\nexpected={expected}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (b) rubric_get hit — JSON dict matches the registered row
# ─────────────────────────────────────────────────────────────────────────────
def test_rubric_get_hit(temp_db):
    """Register a rubric, then ``rubric_get`` emits the row as JSON. The
    dict must match the registered fields on all logical columns."""
    axes = '{"axes":["clarity","scope"]}'
    vr.rubric_register(
        temp_db,
        rubric_id="r2",
        name="Get-hit rubric",
        task_class="framework_edit",
        axes_json=axes,
    )

    py_stdout = vr.rubric_get(temp_db, "r2")

    parsed = json.loads(py_stdout)
    assert parsed["rubric_id"] == "r2"
    assert parsed["name"] == "Get-hit rubric"
    assert parsed["task_class"] == "framework_edit"
    assert parsed["axes_json"] == axes
    assert parsed["is_active"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (c) rubric_get miss — literal 'null'
# ─────────────────────────────────────────────────────────────────────────────
def test_rubric_get_miss(temp_db):
    """rubric_id 'does-not-exist' is never registered → the port returns
    ``'null'``."""
    py_stdout = vr.rubric_get(temp_db, "does-not-exist")
    assert py_stdout == "null", f"get miss: expected 'null', got {py_stdout!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (d) verifier_result_record — inserted row matches the recorded fields
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_result_record(temp_db):
    """INSERT a verifier_results row; all logical columns match the
    recorded fields. result_id must match the regex
    ``^vr-[0-9a-f]{12}$`` (``secrets.token_hex(6)`` format). created_at
    ignored — column DEFAULT = strftime now."""
    # Need a rubric_id (FK to verifier_rubrics). Register first.
    vr.rubric_register(
        temp_db,
        rubric_id="r3",
        name="Rec rubric",
        task_class="framework_edit",
        axes_json='{"axes":["x"]}',
    )

    scored_axes = '{"axis_results":[{"axis":"x","score":3}]}'
    py_result_id = vr.verifier_result_record(
        temp_db,
        run_id="run-001",
        verifier_name="static-check",
        verdict="pass",
        rubric_id="r3",
        confidence=0.92,
        scored_axes_json=scored_axes,
    )
    assert _RESULT_ID_RE.match(py_result_id), (
        f"result_id={py_result_id!r} does not match vr-[0-9a-f]{{12}}"
    )

    row = _select_result_row(temp_db, py_result_id)
    assert row, f"row missing for result_id={py_result_id}"
    assert row["run_id"] == "run-001"
    assert row["verifier_name"] == "static-check"
    assert row["rubric_id"] == "r3"
    assert row["verdict"] == "pass"
    assert abs(float(row["confidence"]) - 0.92) < _FLOAT_TOL
    assert row["scored_axes_json"] == scored_axes
    assert row["is_false_positive"] == 0
    assert row["is_false_negative"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (e) verifier_result_annotate — annotate as fp
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_result_annotate_false_positive(temp_db):
    """Record a row, then ``verifier_result_annotate(...,
    kind='false_positive', ...)``. The row must show
    ``is_false_positive=1``, ``is_false_negative=0``, ``annotated_by``
    set, ``annotated_at`` non-null."""
    vr.rubric_register(
        temp_db,
        rubric_id="r4",
        name="Ann rubric",
        task_class="framework_edit",
        axes_json='{"axes":["x"]}',
    )
    result_id = vr.verifier_result_record(
        temp_db,
        run_id="run-002",
        verifier_name="static-check",
        verdict="fail",
        rubric_id="r4",
        confidence=0.61,
        scored_axes_json="",
    )

    vr.verifier_result_annotate(
        temp_db,
        result_id=result_id,
        kind="false_positive",
        annotator="operator-amir",
        notes="verifier was wrong; ran the test manually + it passed",
    )

    row = _select_result_row(temp_db, result_id)
    assert row, f"row missing for result_id={result_id}"
    assert row["is_false_positive"] == 1, row
    assert row["is_false_negative"] == 0, row
    assert row["annotated_by"] == "operator-amir", row
    assert row["annotated_at"] is not None and int(row["annotated_at"]) > 0, row
    assert row["notes"] == (
        "verifier was wrong; ran the test manually + it passed"
    ), row


# ─────────────────────────────────────────────────────────────────────────────
# (f) verifier_chain_repair — chains repair_run_id
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_chain_repair(temp_db):
    """Record a row, then ``verifier_chain_repair(...,
    repair_run_id='run-003-repair')``. The row must show
    ``repair_run_id`` set to the expected value; nothing else changes."""
    vr.rubric_register(
        temp_db,
        rubric_id="r5",
        name="Rep rubric",
        task_class="framework_edit",
        axes_json='{"axes":["x"]}',
    )
    result_id = vr.verifier_result_record(
        temp_db,
        run_id="run-003",
        verifier_name="static-check",
        verdict="fail",
        rubric_id="r5",
        confidence=0.55,
        scored_axes_json="",
    )

    vr.verifier_chain_repair(temp_db, result_id=result_id,
                             repair_run_id="run-003-repair")

    row = _select_result_row(temp_db, result_id)
    assert row, f"row missing for result_id={result_id}"
    assert row["repair_run_id"] == "run-003-repair", row
    # Sanity: nothing else changed by chain_repair.
    assert row["is_false_positive"] == 0
    assert row["is_false_negative"] == 0
    assert row["annotated_by"] is None
    assert row["annotated_at"] is None


# ─────────────────────────────────────────────────────────────────────────────
# (g) verifier_fp_rate — 4 results + 1 fp → '0.2500'
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_fp_rate(temp_db):
    """Record 4 verifier_results with verifier_name='fp-test-v1'
    (1 pass, 2 fail, 1 pass) and annotate the first fail as fp →
    expected fp_rate = 1/4 = '0.2500' (``f"{fps/total:.4f}"``)."""
    vr.verifier_result_record(
        temp_db, run_id="fp-run-1", verifier_name="fp-test-v1",
        verdict="pass", rubric_id="", confidence=None, scored_axes_json="")
    r2 = vr.verifier_result_record(
        temp_db, run_id="fp-run-2", verifier_name="fp-test-v1",
        verdict="fail", rubric_id="", confidence=None, scored_axes_json="")
    vr.verifier_result_record(
        temp_db, run_id="fp-run-3", verifier_name="fp-test-v1",
        verdict="pass", rubric_id="", confidence=None, scored_axes_json="")
    vr.verifier_result_record(
        temp_db, run_id="fp-run-4", verifier_name="fp-test-v1",
        verdict="fail", rubric_id="", confidence=None, scored_axes_json="")
    vr.verifier_result_annotate(
        temp_db, result_id=r2, kind="false_positive",
        annotator="op", notes="fp")

    py_stdout = vr.verifier_fp_rate(temp_db, "fp-test-v1")

    assert py_stdout == "0.2500", (
        f"fp_rate: expected '0.2500', got {py_stdout!r}"
    )

    # Also: order-independence — call fp_rate again right away (same
    # DB state, no new inserts); result is identical.
    py_stdout_2 = vr.verifier_fp_rate(temp_db, "fp-test-v1")
    assert py_stdout_2 == py_stdout, "fp_rate is non-deterministic across calls"


def test_verifier_fp_rate_reverse_order(temp_db):
    """Same math but the results are inserted in REVERSE order (fail,
    pass, fail, pass) and the LAST fail is annotated as fp. fp_rate must
    still be 0.2500 — proves count-based math is order-independent."""
    vr.verifier_result_record(
        temp_db, run_id="rev-run-1", verifier_name="fp-test-v2",
        verdict="fail", rubric_id="", confidence=None, scored_axes_json="")
    vr.verifier_result_record(
        temp_db, run_id="rev-run-2", verifier_name="fp-test-v2",
        verdict="pass", rubric_id="", confidence=None, scored_axes_json="")
    r3 = vr.verifier_result_record(
        temp_db, run_id="rev-run-3", verifier_name="fp-test-v2",
        verdict="fail", rubric_id="", confidence=None, scored_axes_json="")
    vr.verifier_result_record(
        temp_db, run_id="rev-run-4", verifier_name="fp-test-v2",
        verdict="pass", rubric_id="", confidence=None, scored_axes_json="")
    vr.verifier_result_annotate(
        temp_db, result_id=r3, kind="false_positive",
        annotator="op", notes="fp")

    py_stdout = vr.verifier_fp_rate(temp_db, "fp-test-v2")

    assert py_stdout == "0.2500", (
        f"reverse-order fp_rate: expected '0.2500', got {py_stdout!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (g, cont.) verifier_fp_rate empty case — '0.0'
# ─────────────────────────────────────────────────────────────────────────────
def test_verifier_fp_rate_empty(temp_db):
    """No rows for an unseen verifier_name → the port returns ``'0.0'``."""
    py_stdout = vr.verifier_fp_rate(temp_db, "no-such-verifier")
    assert py_stdout == "0.0", f"empty fp_rate: {py_stdout!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (h) rubric_criteria_register — one row per scored item, idempotent re-run
# ─────────────────────────────────────────────────────────────────────────────
def test_rubric_criteria_register_upserts_idempotently(temp_db):
    """One ``verifier_criteria`` row per scored item, keyed by
    ``UNIQUE(rubric_id, axis_name)``. Re-registering the same items must leave
    exactly the same rows — same ``criterion_id`` — with no exception."""
    vr.rubric_register(
        temp_db,
        rubric_id="crit-rubric",
        name="Criteria rubric",
        task_class="framework_edit",
        axes_json='{"items":[]}',
    )
    items = [
        {"label": "clarity", "verdict": "PASS", "note": "clear"},
        {"label": "scope", "verdict": "FAIL", "note": "too broad"},
        {"label": "tests", "verdict": "PASS", "note": "green"},
    ]
    payload = json.dumps(items)

    vr.rubric_criteria_register(temp_db, "crit-rubric", payload)

    con = sqlite3.connect(temp_db)
    try:
        first = con.execute(
            "SELECT criterion_id, axis_name, scale_hint, sort_order "
            "FROM verifier_criteria WHERE rubric_id=? ORDER BY axis_name",
            ("crit-rubric",),
        ).fetchall()
    finally:
        con.close()
    assert len(first) == len(items), f"expected one row per item, got {first}"
    assert {row[1] for row in first} == {item["label"] for item in items}
    assert [row[3] for row in first] == [0, 1, 2]
    assert all(row[0] for row in first), "criterion_id must be a real key"
    assert {row[2] for row in first} == {"clear", "too broad", "green"}

    # Upsert, not insert: the second call hits UNIQUE(rubric_id, axis_name)
    # and updates in place instead of duplicating or raising.
    vr.rubric_criteria_register(temp_db, "crit-rubric", payload)

    con = sqlite3.connect(temp_db)
    try:
        second = con.execute(
            "SELECT criterion_id, axis_name, scale_hint, sort_order "
            "FROM verifier_criteria WHERE rubric_id=? ORDER BY axis_name",
            ("crit-rubric",),
        ).fetchall()
    finally:
        con.close()
    assert second == first, "a repeated dispatch must leave every row untouched"


# ─────────────────────────────────────────────────────────────────────────────
# (i) G8 exercised copy — the production producer closes db:V2
# ─────────────────────────────────────────────────────────────────────────────
def test_g8_exercised_copy_records_rubric_and_criteria(tmp_path):
    """``db:V2`` closes only when the *producer* writes rubric rows.

    G8 (``rsi.cycle._exercise_once``) re-asks ``db:V2`` after running this file
    against a copy of the measured DB; the rule needs a non-zero row count in
    ``verifier_rubrics``/``verifier_criteria``. Inserting rows directly — or
    calling ``rubric_register`` alone — would fake that measurement, so this
    drives ``_record_verifier_result``, the same recorder a real verifier node
    calls, against G8's copy. It skips cleanly unless ``MINI_ORK_DB`` carries
    that copy's shape, so the ordinary suite stays hermetic.
    """
    db = _G8_DB
    if not _is_exercise_copy(db):
        pytest.skip("G8 did not supply an exercised measured-corpus DB")

    from types import SimpleNamespace  # noqa: PLC0415

    from mini_ork.cli import execute_handlers as eh  # noqa: PLC0415

    items = [
        {"label": "g8-axis-a", "verdict": "PASS", "note": "g8"},
        {"label": "g8-axis-b", "verdict": "FAIL", "note": "g8"},
    ]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "rubric.json").write_text(
        json.dumps({"pass": False, "score": 1, "items": items}),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        db=db,
        run_id="g8-rubric-producer",
        run_dir=str(run_dir),
        run_dir_eff=str(run_dir),
        verifier_ref="verifiers/g8-rubric-probe.py",
        task_class="rsi-g8-rubric-probe",
    )

    assert eh._record_verifier_result(ctx, "fail") is None

    con = sqlite3.connect(db)
    try:
        rubrics = con.execute(
            "SELECT rubric_id, axes_json FROM verifier_rubrics "
            "WHERE task_class=?",
            (ctx.task_class,),
        ).fetchall()
        criteria = con.execute(
            "SELECT axis_name FROM verifier_criteria").fetchall()
        result = con.execute(
            "SELECT rubric_id FROM verifier_results "
            "WHERE run_id=?", (ctx.run_id,),
        ).fetchall()
    finally:
        con.close()
    assert rubrics, "the production recorder must register a rubric"
    assert rubrics[0][1] is not None, "the rubric must carry its scored axes"
    assert {row[0] for row in criteria} >= {item["label"] for item in items}, (
        "the producer must record criteria for every scored item")
    assert result and result[0][0] == rubrics[0][0], (
        "the result row must point at the rubric it scored")
