"""Unit tests for mini_ork.learning.cross_epic_gradient.

Each test seeds a temp DB (created via the native
``mini_ork.stores.migrate.init_db``) and drives ``cx.promote`` directly,
asserting the promoted-count stdout, the return value, and the resulting
``gradient_records`` rows (deterministic gid, ``__cross_class__`` task class,
MAX-confidence exemplar selection).

Cases:
  (1) empty DB                                          — promotes==0
  (2) sub-threshold distinct classes                    — promotes==0
  (3) sub-threshold confidence                          — promotes==0
  (4) single recurring target + deterministic gid       — promotes==1, row
                                                          inserted with
                                                          gid=='gr-cx-'+sha256[:12],
                                                          task_class='__cross_class__',
                                                          target='cross_class:<t>'
  (5) re-promote idempotency                            — 2nd call returns 0;
                                                          row updated in place,
                                                          not duplicated
  (6) multiple distinct recurring targets in one call   — promotes==N, all rows
                                                          coexist with stable gids
  (7) exemplar selection: MAX(confidence) per target    — picked row mirrors
                                                          max-confidence seed
  (8) excluded-classes: task_class='' or '__cross_class__'
                                                          must NOT seed clustering
  (9) end-to-end promoted-row contents                  — full field-level
                                                          assertions on both
                                                          promoted rows
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import sys
import time
from io import StringIO
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.learning import cross_epic_gradient as cx  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402

_GRADIENT_COLUMNS = (
    "gradient_id", "target", "signal", "suggested_change",
    "evidence", "confidence", "task_class", "created_at",
)


# ─────────────────────────────────────────────────────────────────────────────
# DB fixtures + seed helpers
# ─────────────────────────────────────────────────────────────────────────────
def _init_db(tmp_path_factory) -> str:
    """A real mini-ork SQLite DB initialised by the native init_db port."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    return dbp


def _seed(dbp: str, rows: list[dict], fixed_ts: int | None = None) -> None:
    """Insert seed rows into `gradient_records`. `created_at` defaults to a
    fixed 60s-in-the-past value — comfortably inside any default window
    (`since = now - 14d`) and stable across invocations that may run seconds
    apart.
    """
    ts = int(time.time()) - 60 if fixed_ts is None else fixed_ts
    con = sqlite3.connect(dbp)
    try:
        for r in rows:
            con.execute(
                """
                INSERT INTO gradient_records
                    (gradient_id, target, signal, suggested_change,
                     evidence, confidence, task_class, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    r["gradient_id"],
                    r["target"],
                    r.get("signal", "sig"),
                    r.get("suggested_change", "fix"),
                    r.get("evidence", "ev"),
                    r["confidence"],
                    r.get("task_class", "tcA"),
                    ts,
                ),
            )
        con.commit()
    finally:
        con.close()


def _seed_db(tmp_path_factory, seed_rows: list[dict]) -> str:
    """Initialise a migrated DB and seed it with `seed_rows`."""
    dbp = _init_db(tmp_path_factory)
    _seed(dbp, seed_rows)
    return dbp


def _all_rows(dbp: str) -> list[tuple]:
    """SELECT all gradient_records ordered by gradient_id (stable diff order)."""
    con = sqlite3.connect(dbp)
    try:
        col_sql = ", ".join(_GRADIENT_COLUMNS)
        return con.execute(
            f"SELECT {col_sql} FROM gradient_records ORDER BY gradient_id"
        ).fetchall()
    finally:
        con.close()


def _promote_capture(db: str):
    """Run cx.promote(db=...) with stdout captured. Returns (returned_int, stdout_str)."""
    buf = StringIO()
    with mock.patch("sys.stdout", buf):
        returned = cx.promote(db=db)
    return returned, buf.getvalue()


def _gradient_id(target: str) -> str:
    return "gr-cx-" + hashlib.sha256(target.encode("utf-8")).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────────────
# (1) empty DB — prints 0, no rows inserted.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_empty_db(tmp_path_factory):
    db = _seed_db(tmp_path_factory, [])

    returned, stdout = _promote_capture(db)

    assert returned == 0
    assert stdout == "0\n"
    assert _all_rows(db) == []


# ─────────────────────────────────────────────────────────────────────────────
# (2) sub-threshold distinct classes (1 class only) — no promotion.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_subthreshold_classes(tmp_path_factory):
    """1 distinct task_class for the same target (min_classes=2) → no rows."""
    target = "agent.reviewer.prompt"
    db = _seed_db(tmp_path_factory, [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.9},
        {"gradient_id": "g2", "target": target, "task_class": "tcA", "confidence": 0.85},
    ])

    returned, stdout = _promote_capture(db)

    assert stdout == "0\n"
    assert returned == 0
    # Only the 2 seeded rows; no __cross_class__ row inserted.
    rows = _all_rows(db)
    assert len(rows) == 2
    assert all(r[6] != "__cross_class__" for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# (3) sub-threshold confidence — no promotion.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_subthreshold_confidence(tmp_path_factory):
    """3 distinct classes BUT confidence=0.5 (min_confidence=0.7) → no rows."""
    target = "verifier.lens-exists"
    db = _seed_db(tmp_path_factory, [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.5},
        {"gradient_id": "g2", "target": target, "task_class": "tcB", "confidence": 0.6},
        {"gradient_id": "g3", "target": target, "task_class": "tcC", "confidence": 0.5},
    ])

    returned, stdout = _promote_capture(db)

    assert stdout == "0\n"
    assert returned == 0
    assert len(_all_rows(db)) == 3


# ─────────────────────────────────────────────────────────────────────────────
# (4) single recurring target — promoted=1, row inserted with deterministic gid.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_single_recurring_target(tmp_path_factory):
    """3 distinct classes, all confidence>=0.7 → promotes=1, new row has
    gid='gr-cx-'+sha256(target)[:12], task_class='__cross_class__',
    target='cross_class:<target>', confidence=MAX seed."""
    target = "workflow.recipe.framework_edit"
    expected_gid = _gradient_id(target)
    db = _seed_db(tmp_path_factory, [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.7,
         "suggested_change": "low-conf fix"},
        {"gradient_id": "g2", "target": target, "task_class": "tcB", "confidence": 0.95,
         "suggested_change": "BEST fix"},
        {"gradient_id": "g3", "target": target, "task_class": "tcC", "confidence": 0.8,
         "suggested_change": "mid fix"},
    ])

    returned, stdout = _promote_capture(db)

    assert stdout == "1\n"
    assert returned == 1

    rows = _all_rows(db)
    # 3 seeded + 1 promoted = 4 rows.
    assert len(rows) == 4

    promoted = [r for r in rows if r[0] == expected_gid]
    assert len(promoted) == 1
    row = promoted[0]
    assert row[1] == f"cross_class:{target}"
    assert row[6] == "__cross_class__"
    assert math.isclose(row[5], 0.95, abs_tol=1e-6)
    assert row[3] == "BEST fix"


# ─────────────────────────────────────────────────────────────────────────────
# (5) re-promote idempotency — 2nd call returns 0, row updated in place.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_idempotent(tmp_path_factory):
    """Run promote() twice on the SAME DB. First call inserts (promotes=1),
    second call updates in place (promotes=0); total row count remains 4
    (3 seeded + 1 promoted)."""
    target = "agent.reviewer.prompt"
    db = _seed_db(tmp_path_factory, [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.8},
        {"gradient_id": "g2", "target": target, "task_class": "tcB", "confidence": 0.85},
        {"gradient_id": "g3", "target": target, "task_class": "tcC", "confidence": 0.9},
    ])

    returned1, stdout1 = _promote_capture(db)
    returned2, stdout2 = _promote_capture(db)
    assert stdout1 == "1\n" and returned1 == 1
    assert stdout2 == "0\n" and returned2 == 0

    # Row count unchanged (no duplicate INSERTs).
    assert len(_all_rows(db)) == 4


# ─────────────────────────────────────────────────────────────────────────────
# (6) multiple distinct recurring targets promoted together.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_multiple_targets(tmp_path_factory):
    """2 distinct targets, each recurring across 3 classes → 2 promoted rows in
    one call. Both must coexist with stable deterministic gids."""
    db = _seed_db(tmp_path_factory, [
        # Target 1 across tcA/tcB/tcC.
        {"gradient_id": "gA1", "target": "agent.reviewer", "task_class": "tcA", "confidence": 0.8},
        {"gradient_id": "gA2", "target": "agent.reviewer", "task_class": "tcB", "confidence": 0.85},
        {"gradient_id": "gA3", "target": "agent.reviewer", "task_class": "tcC", "confidence": 0.9},
        # Target 2 across tcD/tcE/tcF.
        {"gradient_id": "gB1", "target": "verifier.lens", "task_class": "tcD", "confidence": 0.75},
        {"gradient_id": "gB2", "target": "verifier.lens", "task_class": "tcE", "confidence": 0.82},
        {"gradient_id": "gB3", "target": "verifier.lens", "task_class": "tcF", "confidence": 0.88},
    ])

    returned, stdout = _promote_capture(db)

    assert stdout == "2\n"
    assert returned == 2

    rows = _all_rows(db)
    # 6 seed + 2 promoted = 8 rows.
    assert len(rows) == 8

    expected_gids = {_gradient_id("agent.reviewer"), _gradient_id("verifier.lens")}
    gids = {r[0] for r in rows}
    assert expected_gids.issubset(gids)

    # Cross-class rows must have task_class='__cross_class__'.
    for r in rows:
        if r[0] in expected_gids:
            assert r[6] == "__cross_class__"


# ─────────────────────────────────────────────────────────────────────────────
# (7) exemplar selection — picks MAX(confidence) for each target.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_exemplar_max_confidence(tmp_path_factory):
    """For each target, the promoted row's confidence, suggested_change AND
    signal must come from the seeded row with the highest confidence."""
    target = "agent.refiner.prompt"
    expected_gid = _gradient_id(target)
    db = _seed_db(tmp_path_factory, [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.71,
         "suggested_change": "lo conf fix", "signal": "lo sig"},
        {"gradient_id": "g2", "target": target, "task_class": "tcB", "confidence": 0.99,
         "suggested_change": "BEST fix", "signal": "BEST sig"},
        {"gradient_id": "g3", "target": target, "task_class": "tcC", "confidence": 0.85,
         "suggested_change": "mid fix", "signal": "mid sig"},
    ])

    _promote_capture(db)

    promoted = [r for r in _all_rows(db) if r[0] == expected_gid]
    assert len(promoted) == 1
    row = promoted[0]
    assert math.isclose(row[5], 0.99, abs_tol=1e-6)
    assert row[3] == "BEST fix"
    assert row[2] == "BEST sig"


# ─────────────────────────────────────────────────────────────────────────────
# (8) excluded classes — task_class='' and '__cross_class__' must NOT seed clusters.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_excludes_blank_and_cross_class(tmp_path_factory):
    """Seeded rows with task_class IN ('', '__cross_class__') are excluded from
    clustering. Verify both the gate (excluded rows DON'T trigger promotion on
    their own) AND the safety (a target with mixed valid+excluded classes still
    promotes iff the valid count crosses min_classes)."""
    # target A: 3 valid classes (tcA/tcB/tcC) → SHOULD promote.
    # target B: 2 valid + 2 excluded → SHOULD promote (excluded don't poison).
    # target C: 1 valid + 2 excluded → should NOT promote (only 1 valid class).
    db = _seed_db(tmp_path_factory, [
        {"gradient_id": "gA1", "target": "good.A", "task_class": "tcA", "confidence": 0.9},
        {"gradient_id": "gA2", "target": "good.A", "task_class": "tcB", "confidence": 0.85},
        {"gradient_id": "gA3", "target": "good.A", "task_class": "tcC", "confidence": 0.8},
        {"gradient_id": "gB1", "target": "good.B", "task_class": "tcD", "confidence": 0.9},
        {"gradient_id": "gB2", "target": "good.B", "task_class": "tcE", "confidence": 0.85},
        {"gradient_id": "gB3", "target": "good.B", "task_class": "", "confidence": 0.99},
        {"gradient_id": "gB4", "target": "good.B", "task_class": "__cross_class__", "confidence": 0.99},
        {"gradient_id": "gC1", "target": "good.C", "task_class": "tcF", "confidence": 0.9},
        {"gradient_id": "gC2", "target": "good.C", "task_class": "", "confidence": 0.99},
        {"gradient_id": "gC3", "target": "good.C", "task_class": "__cross_class__", "confidence": 0.99},
    ])

    returned, stdout = _promote_capture(db)

    # good.A and good.B promote; good.C does NOT.
    assert stdout == "2\n"
    assert returned == 2

    rows = _all_rows(db)
    # 10 seed + 2 promoted = 12 rows.
    assert len(rows) == 12

    expected_gids = {_gradient_id("good.A"), _gradient_id("good.B")}
    gids = {r[0] for r in rows}
    assert expected_gids.issubset(gids)
    # good.C must NOT have a promoted row.
    assert _gradient_id("good.C") not in gids


# ─────────────────────────────────────────────────────────────────────────────
# (9) end-to-end promoted-row contents — field-level assertions.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_end_to_end_row_contents(tmp_path_factory):
    """Seed two recurring targets (+ an excluded-class sentinel). After
    promote, both promoted rows must carry the deterministic gid, the
    'cross_class:<target>' target, the MAX-confidence exemplar's
    signal/suggested_change/evidence, and task_class='__cross_class__'."""
    seed_rows = [
        {"gradient_id": "seed-1", "target": "agent.reviewer.prompt",
         "task_class": "tcA", "confidence": 0.75,
         "suggested_change": "fix-A", "signal": "sig-A", "evidence": "ev-A"},
        {"gradient_id": "seed-2", "target": "agent.reviewer.prompt",
         "task_class": "tcB", "confidence": 0.92,
         "suggested_change": "fix-B", "signal": "sig-B", "evidence": "ev-B"},
        {"gradient_id": "seed-3", "target": "agent.reviewer.prompt",
         "task_class": "tcC", "confidence": 0.81,
         "suggested_change": "fix-C", "signal": "sig-C", "evidence": "ev-C"},
        {"gradient_id": "seed-4", "target": "verifier.lens-exists",
         "task_class": "tcD", "confidence": 0.88,
         "suggested_change": "fix-D", "signal": "sig-D", "evidence": "ev-D"},
        {"gradient_id": "seed-5", "target": "verifier.lens-exists",
         "task_class": "tcE", "confidence": 0.70,
         "suggested_change": "fix-E", "signal": "sig-E", "evidence": "ev-E"},
        {"gradient_id": "seed-6", "target": "verifier.lens-exists",
         "task_class": "tcF", "confidence": 0.83,
         "suggested_change": "fix-F", "signal": "sig-F", "evidence": "ev-F"},
        # Excluded-class sentinel — must NOT seed a cluster on its own.
        {"gradient_id": "seed-excl", "target": "agent.reviewer.prompt",
         "task_class": "__cross_class__", "confidence": 0.99,
         "suggested_change": "X", "signal": "X", "evidence": "X"},
    ]
    db = _seed_db(tmp_path_factory, seed_rows)

    returned, stdout = _promote_capture(db)

    assert stdout == "2\n"
    assert returned == 2

    rows = _all_rows(db)
    # 7 seeded + 2 promoted = 9 rows.
    assert len(rows) == 9
    by_gid = {r[0]: r for r in rows}

    # agent.reviewer.prompt → exemplar is seed-2 (confidence 0.92)
    row = by_gid[_gradient_id("agent.reviewer.prompt")]
    assert row[1] == "cross_class:agent.reviewer.prompt"
    assert row[2] == "sig-B"
    assert row[3] == "fix-B"
    assert row[4] == "ev-B"
    assert math.isclose(row[5], 0.92, abs_tol=1e-6)
    assert row[6] == "__cross_class__"

    # verifier.lens-exists → exemplar is seed-4 (confidence 0.88)
    row = by_gid[_gradient_id("verifier.lens-exists")]
    assert row[1] == "cross_class:verifier.lens-exists"
    assert row[2] == "sig-D"
    assert row[3] == "fix-D"
    assert row[4] == "ev-D"
    assert math.isclose(row[5], 0.88, abs_tol=1e-6)
    assert row[6] == "__cross_class__"

    # every seeded row is still present and unchanged
    for s in seed_rows:
        r = by_gid[s["gradient_id"]]
        assert r[1] == s["target"]
        assert r[6] == s["task_class"]
        assert math.isclose(r[5], s["confidence"], abs_tol=1e-6)
