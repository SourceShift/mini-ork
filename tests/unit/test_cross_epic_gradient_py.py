"""Parity gate: mini_ork.ported.cross_epic_gradient vs lib/cross_epic_gradient.sh.

Each test seeds a temp DB (created via db/init.sh — kickoff requirement) and
invokes the LIVE bash subprocess on a sibling DB; the Python port is called on
a parallel DB copy. Row content (`SELECT * FROM gradient_records ORDER BY
gradient_id`) must match byte-for-byte (confidence at 1e-6 tolerance). No mocks,
no hardcoded expected outputs — expected is always derived from a control bash
invocation that shares the inputs and the seed.

Run pattern: bash-side and python-side each get a private DB seeded
identically BEFORE either side runs. This prevents the first runner's INSERT
from corrupting the second runner's expected INSERT branch (deterministic gid
turns the second call into an UPDATE).

>=6 cases:
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
  (9) end-to-end LIVE-bash-vs-python parity             — same seed into two DB
                                                          copies, run each side,
                                                          SELECT * ordered by gid,
                                                          assert byte-equal
                                                          row tuples (confidence
                                                          1e-6).
"""
from __future__ import annotations

import hashlib
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import cross_epic_gradient as cx  # noqa: E402

SH = REPO / "lib" / "cross_epic_gradient.sh"
INIT_SH = REPO / "db" / "init.sh"

_GRADIENT_COLUMNS = (
    "gradient_id", "target", "signal", "suggested_change",
    "evidence", "confidence", "task_class", "created_at",
)


def _which(*tools: str) -> dict[str, str]:
    out = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


# ─────────────────────────────────────────────────────────────────────────────
# DB fixtures + seed helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path_factory):
    """A real mini-ork SQLite DB initialised by db/init.sh — kickoff explicitly
    requires 'temp DB created by db/init.sh'."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _seed(dbp: str, rows: list[dict], fixed_ts: int | None = None) -> None:
    """Insert seed rows into `gradient_records`. `created_at` defaults to a
    fixed 60s-in-the-past value — comfortably inside any default window
    (`since = now - 14d`), and stable across bash-side and python-side
    invocations that may run seconds apart.

    Pass an explicit `fixed_ts` to guarantee byte-identical `created_at`
    across multiple `_seed` calls in the same test (otherwise the two
    sequential seed calls can drift by a few seconds, breaking tests that
    do strict tuple equality on seeded rows).
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


def _seed_db_with_copy(
    tmp_path_factory, seed_rows: list[dict]
) -> tuple[str, str]:
    """Initialise two identical DBs (bash-side + python-side) and seed them
    with the same rows. Returns (bash_db, py_db). Each side gets a private
    DB BEFORE either runs, so the first runner's INSERT does not corrupt
    the second runner's expected INSERT branch.

    Both DBs are seeded with the SAME `created_at` so seeded rows match
    byte-for-byte across the two DBs (any drift between two `int(time.time())`
    reads would break strict tuple equality).
    """
    shared_ts = int(time.time()) - 60
    home = tmp_path_factory.mktemp("home")
    bash_db = str(home / "bash.db")
    py_db = str(home / "py.db")
    for dbp in (bash_db, py_db):
        subprocess.run(
            ["bash", str(INIT_SH)],
            env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
            capture_output=True, text=True, check=True,
        )
        _seed(dbp, seed_rows, fixed_ts=shared_ts)
    return bash_db, py_db


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


def _bash(db: str, snippet: str) -> subprocess.CompletedProcess:
    """Source lib/cross_epic_gradient.sh and run `snippet` (a single shell
    statement that calls cross_epic_gradient_promote). The bash function prints
    promoted-count on stdout — we do NOT strip trailing newlines, so the
    parity check on `promoted` matches byte-for-byte."""
    bash_wrapper = f'. "{SH}"\n{snippet}\n'
    return subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={
            **os.environ,
            "MINI_ORK_DB": db,
            "MINI_ORK_ROOT": str(REPO),
            "MINI_ORK_HOME": str(REPO),
        },
        capture_output=True, text=True,
    )


def _promote_capture(db: str):
    """Run cx.promote(db=...) with stdout captured. Returns (returned_int, stdout_str)."""
    buf = StringIO()
    with mock.patch("sys.stdout", buf):
        returned = cx.promote(db=db)
    return returned, buf.getvalue()


def _gradient_id(target: str) -> str:
    return "gr-cx-" + hashlib.sha256(target.encode("utf-8")).hexdigest()[:12]


def _row_parity(bash_row: tuple, py_row: tuple) -> None:
    """Compare two rows field-by-field. Confidence at 1e-6, created_at ignored
    (both sides use int(time.time()) at INSERT time but the calls may run
    seconds apart)."""
    assert len(bash_row) == len(py_row) == 8
    fields = ("gradient_id", "target", "signal", "suggested_change",
              "evidence", "confidence", "task_class", "created_at")
    for i, f in enumerate(fields):
        if f == "confidence":
            assert math.isclose(bash_row[i], py_row[i], abs_tol=1e-6), (
                f"confidence drift: bash={bash_row[i]} py={py_row[i]}"
            )
        elif f == "created_at":
            continue  # live-clock; both sides use time.time() but may drift
        else:
            assert bash_row[i] == py_row[i], (
                f"{f}: bash={bash_row[i]!r} py={py_row[i]!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# (1) empty DB — bash and python both print 0, no rows inserted.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_empty_db(tmp_path_factory):
    """Empty DB → bash prints 0, python prints 0, gradient_records is empty on
    both sides (no rows inserted)."""
    _which("bash", "python3")
    bash_db, py_db = _seed_db_with_copy(tmp_path_factory, [])

    rb = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb.returncode == 0, rb.stderr

    py_returned, py_stdout = _promote_capture(py_db)

    assert py_returned == 0
    assert rb.stdout == py_stdout == "0\n"
    assert _all_rows(bash_db) == _all_rows(py_db) == []


# ─────────────────────────────────────────────────────────────────────────────
# (2) sub-threshold distinct classes (1 class only) — no promotion.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_subthreshold_classes(tmp_path_factory):
    """1 distinct task_class for the same target (min_classes=2) → no rows."""
    _which("bash", "python3")
    target = "agent.reviewer.prompt"
    seed_rows = [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.9},
        {"gradient_id": "g2", "target": target, "task_class": "tcA", "confidence": 0.85},
    ]
    bash_db, py_db = _seed_db_with_copy(tmp_path_factory, seed_rows)

    rb = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb.returncode == 0
    py_returned, _ = _promote_capture(py_db)

    assert rb.stdout == "0\n"
    assert py_returned == 0
    # Only the 2 seeded rows on each side; no __cross_class__ row inserted.
    bash_rows = _all_rows(bash_db)
    py_rows = _all_rows(py_db)
    assert len(bash_rows) == 2 == len(py_rows)
    assert bash_rows == py_rows  # seed identical, no INSERTs
    assert all(r[6] != "__cross_class__" for r in bash_rows)
    assert all(r[6] != "__cross_class__" for r in py_rows)


# ─────────────────────────────────────────────────────────────────────────────
# (3) sub-threshold confidence — no promotion.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_subthreshold_confidence(tmp_path_factory):
    """3 distinct classes BUT confidence=0.5 (min_confidence=0.7) → no rows."""
    _which("bash", "python3")
    target = "verifier.lens-exists"
    seed_rows = [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.5},
        {"gradient_id": "g2", "target": target, "task_class": "tcB", "confidence": 0.6},
        {"gradient_id": "g3", "target": target, "task_class": "tcC", "confidence": 0.5},
    ]
    bash_db, py_db = _seed_db_with_copy(tmp_path_factory, seed_rows)

    rb = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb.returncode == 0
    py_returned, _ = _promote_capture(py_db)

    assert rb.stdout == "0\n"
    assert py_returned == 0
    assert _all_rows(bash_db) == _all_rows(py_db)
    assert len(_all_rows(bash_db)) == 3


# ─────────────────────────────────────────────────────────────────────────────
# (4) single recurring target — promoted=1, row inserted with deterministic gid.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_single_recurring_target(tmp_path_factory):
    """3 distinct classes, all confidence>=0.7 → promotes=1, new row has
    gid='gr-cx-'+sha256(target)[:12], task_class='__cross_class__',
    target='cross_class:<target>', confidence=MAX seed."""
    _which("bash", "python3")
    target = "workflow.recipe.framework_edit"
    expected_gid = _gradient_id(target)
    seed_rows = [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.7,
         "suggested_change": "low-conf fix"},
        {"gradient_id": "g2", "target": target, "task_class": "tcB", "confidence": 0.95,
         "suggested_change": "BEST fix"},
        {"gradient_id": "g3", "target": target, "task_class": "tcC", "confidence": 0.8,
         "suggested_change": "mid fix"},
    ]
    bash_db, py_db = _seed_db_with_copy(tmp_path_factory, seed_rows)

    rb = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb.returncode == 0, rb.stderr

    py_returned, py_stdout = _promote_capture(py_db)

    assert rb.stdout == py_stdout == "1\n"
    assert py_returned == 1

    bash_rows = _all_rows(bash_db)
    py_rows = _all_rows(py_db)
    # 3 seeded + 1 promoted = 4 rows on each side.
    assert len(bash_rows) == 4 == len(py_rows)

    bash_promoted = [r for r in bash_rows if r[0] == expected_gid]
    py_promoted = [r for r in py_rows if r[0] == expected_gid]
    assert len(bash_promoted) == 1 == len(py_promoted)

    # Content sanity on the bash-side promoted row (python mirror is asserted below).
    assert bash_promoted[0][0] == expected_gid
    assert bash_promoted[0][1] == f"cross_class:{target}"
    assert bash_promoted[0][6] == "__cross_class__"
    assert math.isclose(bash_promoted[0][5], 0.95, abs_tol=1e-6)
    assert bash_promoted[0][3] == "BEST fix"

    # Pairwise parity between bash row and python row.
    _row_parity(bash_promoted[0], py_promoted[0])


# ─────────────────────────────────────────────────────────────────────────────
# (5) re-promote idempotency — 2nd call returns 0, row updated in place.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_idempotent(tmp_path_factory):
    """Run promote() twice on the SAME DB. First call inserts (promotes=1),
    second call updates in place (promotes=0); total row count remains 4
    (3 seeded + 1 promoted). Tested independently for bash and for python."""
    _which("bash", "python3")
    target = "agent.reviewer.prompt"
    seed_rows = [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.8},
        {"gradient_id": "g2", "target": target, "task_class": "tcB", "confidence": 0.85},
        {"gradient_id": "g3", "target": target, "task_class": "tcC", "confidence": 0.9},
    ]

    # ── Bash side ──
    bash_db, _ = _seed_db_with_copy(tmp_path_factory, seed_rows)
    rb1 = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb1.returncode == 0
    assert rb1.stdout == "1\n"
    rb2 = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb2.returncode == 0
    assert rb2.stdout == "0\n"

    # ── Python side (parallel fresh DB seeded identically) ──
    _, py_db = _seed_db_with_copy(tmp_path_factory, seed_rows)
    py_returned1, py_stdout1 = _promote_capture(py_db)
    py_returned2, py_stdout2 = _promote_capture(py_db)
    assert py_stdout1 == "1\n" and py_returned1 == 1
    assert py_stdout2 == "0\n" and py_returned2 == 0

    # Row count unchanged (no duplicate INSERTs) on python side.
    py_rows_after = _all_rows(py_db)
    assert len(py_rows_after) == 4


# ─────────────────────────────────────────────────────────────────────────────
# (6) multiple distinct recurring targets promoted together.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_multiple_targets(tmp_path_factory):
    """2 distinct targets, each recurring across 3 classes → 2 promoted rows in
    one call. Both must coexist with stable deterministic gids."""
    _which("bash", "python3")
    seed_rows = [
        # Target 1 across tcA/tcB/tcC.
        {"gradient_id": "gA1", "target": "agent.reviewer", "task_class": "tcA", "confidence": 0.8},
        {"gradient_id": "gA2", "target": "agent.reviewer", "task_class": "tcB", "confidence": 0.85},
        {"gradient_id": "gA3", "target": "agent.reviewer", "task_class": "tcC", "confidence": 0.9},
        # Target 2 across tcD/tcE/tcF.
        {"gradient_id": "gB1", "target": "verifier.lens", "task_class": "tcD", "confidence": 0.75},
        {"gradient_id": "gB2", "target": "verifier.lens", "task_class": "tcE", "confidence": 0.82},
        {"gradient_id": "gB3", "target": "verifier.lens", "task_class": "tcF", "confidence": 0.88},
    ]
    bash_db, py_db = _seed_db_with_copy(tmp_path_factory, seed_rows)

    rb = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb.returncode == 0
    py_returned, py_stdout = _promote_capture(py_db)

    assert rb.stdout == py_stdout == "2\n"
    assert py_returned == 2

    bash_rows = _all_rows(bash_db)
    py_rows = _all_rows(py_db)
    # 6 seed + 2 promoted = 8 rows on each side.
    assert len(bash_rows) == 8 == len(py_rows)

    expected_gids = {_gradient_id("agent.reviewer"), _gradient_id("verifier.lens")}
    bash_gids = {r[0] for r in bash_rows}
    py_gids = {r[0] for r in py_rows}
    assert expected_gids.issubset(bash_gids)
    assert expected_gids.issubset(py_gids)

    # Cross-class rows must have task_class='__cross_class__' on both sides.
    for r in bash_rows:
        if r[0] in expected_gids:
            assert r[6] == "__cross_class__"
    for r in py_rows:
        if r[0] in expected_gids:
            assert r[6] == "__cross_class__"


# ─────────────────────────────────────────────────────────────────────────────
# (7) exemplar selection — picks MAX(confidence) for each target.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_exemplar_max_confidence(tmp_path_factory):
    """For each target, the promoted row's confidence and suggested_change must
    come from the seeded row with the highest confidence."""
    _which("bash", "python3")
    target = "agent.refiner.prompt"
    expected_gid = _gradient_id(target)
    seed_rows = [
        {"gradient_id": "g1", "target": target, "task_class": "tcA", "confidence": 0.71,
         "suggested_change": "lo conf fix", "signal": "lo sig"},
        {"gradient_id": "g2", "target": target, "task_class": "tcB", "confidence": 0.99,
         "suggested_change": "BEST fix", "signal": "BEST sig"},
        {"gradient_id": "g3", "target": target, "task_class": "tcC", "confidence": 0.85,
         "suggested_change": "mid fix", "signal": "mid sig"},
    ]
    bash_db, py_db = _seed_db_with_copy(tmp_path_factory, seed_rows)

    rb = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb.returncode == 0
    _promote_capture(py_db)

    bash_promoted = [r for r in _all_rows(bash_db) if r[0] == expected_gid]
    py_promoted = [r for r in _all_rows(py_db) if r[0] == expected_gid]
    assert len(bash_promoted) == 1 == len(py_promoted)

    _row_parity(bash_promoted[0], py_promoted[0])
    assert math.isclose(bash_promoted[0][5], 0.99, abs_tol=1e-6)
    assert bash_promoted[0][3] == "BEST fix"


# ─────────────────────────────────────────────────────────────────────────────
# (8) excluded classes — task_class='' and '__cross_class__' must NOT seed clusters.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_excludes_blank_and_cross_class(tmp_path_factory):
    """Seeded rows with task_class IN ('', '__cross_class__') are excluded from
    clustering. Verify both the gate (excluded rows DON'T trigger promotion on
    their own) AND the safety (a target with mixed valid+excluded classes still
    promotes iff the valid count crosses min_classes)."""
    _which("bash", "python3")
    # target A: 3 valid classes (tcA/tcB/tcC) → SHOULD promote.
    # target B: 2 valid + 2 excluded → SHOULD promote (excluded don't poison).
    # target C: 1 valid + 2 excluded → should NOT promote (only 1 valid class).
    seed_rows = [
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
    ]
    bash_db, py_db = _seed_db_with_copy(tmp_path_factory, seed_rows)

    rb = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb.returncode == 0
    py_returned, py_stdout = _promote_capture(py_db)

    # good.A and good.B promote; good.C does NOT.
    assert rb.stdout == py_stdout == "2\n"
    assert py_returned == 2

    bash_rows = _all_rows(bash_db)
    py_rows = _all_rows(py_db)
    # 10 seed + 2 promoted = 12 rows on each side.
    assert len(bash_rows) == 12 == len(py_rows)

    expected_gids = {_gradient_id("good.A"), _gradient_id("good.B")}
    bash_gids = {r[0] for r in bash_rows}
    py_gids = {r[0] for r in py_rows}
    assert expected_gids.issubset(bash_gids)
    assert expected_gids.issubset(py_gids)
    # good.C must NOT have a promoted row.
    assert _gradient_id("good.C") not in bash_gids
    assert _gradient_id("good.C") not in py_gids

    # Pairwise parity on the 2 promoted rows.
    for gid in expected_gids:
        b_row = next(r for r in bash_rows if r[0] == gid)
        p_row = next(r for r in py_rows if r[0] == gid)
        _row_parity(b_row, p_row)


# ─────────────────────────────────────────────────────────────────────────────
# (9) end-to-end LIVE-bash-vs-python row parity — gating acceptance test.
# ─────────────────────────────────────────────────────────────────────────────
def test_promote_end_to_end_live_bash_parity(tmp_path_factory):
    """Gating case. Seed the same rows into two DBs. Run bash on one, run
    python on the other. SELECT * ordered by gradient_id on each side and
    assert the row tuples are byte-equal (confidence at 1e-6 tolerance)."""
    _which("bash", "python3")

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

    bash_db, py_db = _seed_db_with_copy(tmp_path_factory, seed_rows)

    rb = _bash(bash_db, "cross_epic_gradient_promote")
    assert rb.returncode == 0, rb.stderr

    py_returned, py_stdout = _promote_capture(py_db)

    # Stdout (integer count): 2 promotions (agent.reviewer.prompt + verifier.lens-exists).
    assert rb.stdout == py_stdout == "2\n"
    assert py_returned == 2

    # Row lists: same length (7 seeded + 2 promoted = 9 on each side).
    bash_rows = _all_rows(bash_db)
    py_rows = _all_rows(py_db)
    assert len(bash_rows) == len(py_rows) == 9, (
        f"row count drift: bash={len(bash_rows)} py={len(py_rows)}"
    )

    # Byte-equality on each paired row. Compare by gradient_id order.
    for b_row, p_row in zip(bash_rows, py_rows):
        _row_parity(b_row, p_row)
