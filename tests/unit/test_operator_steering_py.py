"""Standalone unit tests for ``mini_ork.steering.operator_steering``.

Replaces the bash-parity gate (against ``lib/operator_steering.sh``) as
part of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer invokes the LIVE bash subprocess
— it asserts the port's behaviour directly. The expected values below are
the semantic contract the bash side used to pin (emit validation errors,
row round-trips, fetch ordering/role/consumed/expiry filters, float
precision), now asserted on the port's output.

Eight cases:
  (a) emit happy-path round-trip    — inserted row matches the emit args
                                       field-by-field (id/created_at/
                                       expires_at excluded: timing-dependent)
  (b) emit --message required       — ValueError "--message required"
  (c) emit unknown flag             — ValueError "unknown flag: …"
  (d) fetch_for ordering            — critical > warn > info, tiebreak by
                                       confidence DESC, then created_at DESC
  (e) fetch_for role matching       — OR-of-role_target semantics
                                       (role_target='any' is broadcast;
                                       specific role matches only its own
                                       row + any)
  (f) fetch_for consumed-mark       — second call returns []; rows are
                                       consumed in one statement
  (g) fetch_for expired + pre-consumed filters — expired row and
                                       pre-consumed row are excluded
  (h) float confidence precision    — 0.123456789 round-trips through
                                       emit + fetch_for (1e-6 tolerance)

Environment isolation:
  The shell pytest runs in often has MINI_ORK_DB / MINI_ORK_HOME set to the
  main repo's state.db. Without isolation, ``ops.emit`` / ``ops.fetch_for``
  would read/write THAT db instead of the per-test temp db. Each test calls
  ``_point_python_env(monkeypatch, db, home)`` to redirect the Python
  process via monkeypatch.setenv (auto-revert on test exit). The DB itself
  is seeded via ``mini_ork.stores.migrate.init_db``.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.steering import operator_steering as ops  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

# Columns the tests strip before comparing. These are timing-dependent
# (created_at/expires_at) or implementation-specific (id is AUTOINCREMENT).
_STRIP_KEYS = ("id", "created_at", "expires_at")


def _init_db(tmp_path_factory, *, name: str = "home") -> tuple[str, str]:
    """Spin up a fresh mini-ork SQLite DB via init_db.

    Returns (db_path, home_dir). tmp_path_factory guarantees a unique
    sub-directory per call so two DBs in the same test don't collide.
    """
    home = tmp_path_factory.mktemp(name)
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return dbp, str(home)


def _point_python_env(monkeypatch: pytest.MonkeyPatch, db: str, home: str) -> None:
    """Redirect the Python process's ``_resolve_db`` to the temp db.

    Without this, the port would resolve to whatever MINI_ORK_DB /
    MINI_ORK_HOME is set in the shell pytest runs in (usually the repo's
    main state.db).
    """
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", home)


def _seed_row(
    db: str,
    *,
    run_id: str | None,
    role_target: str,
    severity: str,
    message: str,
    source: str = "",
    confidence: float = 0.8,
    created_at: int | None = None,
    expires_at: int | None = None,
    consumed_at: int | None = None,
) -> int:
    """Direct-SQL insert (bypasses emit so we can craft expired /
    pre-consumed rows for filter tests)."""
    now = int(time.time() * 1000)
    if created_at is None:
        created_at = now
    if expires_at is None:
        expires_at = now + 3600 * 1000
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            """INSERT INTO operator_steering
                 (run_id, role_target, severity, message, source,
                  confidence, created_at, expires_at, consumed_at)
               VALUES (NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?)""",
            (
                run_id if run_id is not None else "",
                role_target,
                severity,
                message,
                source,
                float(confidence),
                int(created_at),
                int(expires_at),
                consumed_at,
            ),
        )
        con.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)
    finally:
        con.close()


def _read_row(db: str, rowid: int) -> dict:
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            """SELECT id, run_id, role_target, severity, message, source,
                      confidence, created_at, expires_at
                 FROM operator_steering WHERE id=?""",
            (rowid,),
        ).fetchone()
    finally:
        con.close()
    assert row is not None, f"row {rowid} not found"
    return {
        "id": row[0],
        "run_id": row[1],
        "role_target": row[2],
        "severity": row[3],
        "message": row[4],
        "source": row[5],
        "confidence": row[6],
        "created_at": row[7],
        "expires_at": row[8],
    }


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _STRIP_KEYS}


# ─────────────────────────────────────────────────────────────────────────────
# (a) emit happy-path round-trip
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_happy_path(tmp_path_factory, monkeypatch):
    """Python ``emit`` inserts a row whose field-level contents (run_id /
    role_target / severity / message / source / confidence) match the emit
    args. id/created_at/expires_at are stripped (AUTOINCREMENT + wall-clock)."""
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)

    py_rowid = ops.emit(
        message="from-py",
        run_id="r-a",
        role_target="implementer",
        severity="info",
        source="py-src",
        confidence=0.7,
    )
    py_row = _read_row(db, py_rowid)

    assert _strip(py_row) == {
        "run_id": "r-a",
        "role_target": "implementer",
        "severity": "info",
        "message": "from-py",
        "source": "py-src",
        "confidence": 0.7,
    }


# ─────────────────────────────────────────────────────────────────────────────
# (b) emit --message required
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_message_required(tmp_path_factory, monkeypatch):
    """Empty message raises ValueError ``--message required``."""
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)

    with pytest.raises(ValueError) as exc_info:
        ops.emit(message="", role_target="implementer")
    assert "operator_steering_emit: --message required" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# (c) emit unknown flag
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_unknown_flag(tmp_path_factory, monkeypatch):
    """An unknown kwarg raises ValueError ``unknown flag: …``."""
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)

    with pytest.raises(ValueError) as exc_info:
        ops.emit(message="x", bogus="y")
    assert "operator_steering_emit: unknown flag: bogus" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# (d) fetch_for ordering — severity tier, confidence DESC, created_at DESC
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_for_ordering(tmp_path_factory, monkeypatch):
    """fetch_for orders by (severity tier DESC, confidence DESC, created_at
    DESC) LIMIT 10. Seed 5 rows with crafted created_at and confidence
    values so the tiebreakers actually fire.

    Seeded rows (all role='any', run_id='r-d', expires_at=fresh, unconsumed):
      row A: severity='info',     confidence=0.50, created_at=t+0
      row B: severity='warn',     confidence=0.95, created_at=t+1000
      row C: severity='critical', confidence=0.30, created_at=t+2000
      row D: severity='critical', confidence=0.95, created_at=t+3000  (newest critical@0.95)
      row E: severity='critical', confidence=0.95, created_at=t+2500  (older critical@0.95 → ties D's confidence)
    Expected order: D, E, C, B, A
      - D first (critical, conf 0.95, newest)
      - E second (critical, conf 0.95, older than D → tiebreak by created_at DESC)
      - C third (critical, conf 0.30)
      - B fourth (warn, conf 0.95)
      - A last (info, conf 0.50)
    """
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)

    now = int(time.time() * 1000)
    seeds = [
        # (role_target, severity, message, confidence, created_at, source)
        ("any", "info", "msg-A", 0.50, now + 0, "seed"),
        ("any", "warn", "msg-B", 0.95, now + 1000, "seed"),
        ("any", "critical", "msg-C", 0.30, now + 2000, "seed"),
        ("any", "critical", "msg-D", 0.95, now + 3000, "seed"),
        ("any", "critical", "msg-E", 0.95, now + 2500, "seed"),
    ]
    for role, sev, msg, conf, ts, src in seeds:
        _seed_row(db, run_id="r-d", role_target=role, severity=sev,
                  message=msg, source=src, confidence=conf,
                  created_at=ts, expires_at=now + 3600 * 1000)

    py_rows = ops.fetch_for("r-d", "any")
    py_stripped = [_strip(row) for row in py_rows]

    assert [r["message"] for r in py_stripped] == [
        "msg-D", "msg-E", "msg-C", "msg-B", "msg-A",
    ]
    # Sanity: the leader is D (critical, conf 0.95, newest).
    assert py_stripped[0]["severity"] == "critical"
    assert py_stripped[0]["confidence"] == 0.95


# ─────────────────────────────────────────────────────────────────────────────
# (e) fetch_for role matching
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_for_role_matching(tmp_path_factory, monkeypatch):
    """fetch_for applies the OR-of-role_target filter:
    role_target = ?  OR  role_target = 'any'.

    Seed: 3 rows with run_id='r-e':
      - role='any'         → visible to every role query
      - role='implementer' → visible to role='implementer' AND to role='any'
                             via the OR 'any' branch
      - role='reviewer'    → visible only to role='reviewer' (plus 'any'
                             via the OR branch)
    """
    db, home = _init_db(tmp_path_factory, name="e1")
    _point_python_env(monkeypatch, db, home)

    def seed_all(d: str) -> None:
        _seed_row(d, run_id="r-e", role_target="any",         severity="info",
                  message="row-any")
        _seed_row(d, run_id="r-e", role_target="implementer", severity="info",
                  message="row-impl")
        _seed_row(d, run_id="r-e", role_target="reviewer",    severity="info",
                  message="row-rev")

    seed_all(db)
    # ask 'implementer' → row-any + row-impl (OR 'any' branch).
    py_impl = sorted(row["message"] for row in ops.fetch_for("r-e", "implementer"))
    assert py_impl == ["row-any", "row-impl"]

    # ask 'reviewer' → row-any + row-rev (fresh DB so the first fetch's
    # consumed marks don't leak in).
    db2, home2 = _init_db(tmp_path_factory, name="e2")
    seed_all(db2)
    monkeypatch.setenv("MINI_ORK_DB", db2)
    monkeypatch.setenv("MINI_ORK_HOME", home2)
    py_rev = sorted(row["message"] for row in ops.fetch_for("r-e", "reviewer"))
    assert py_rev == ["row-any", "row-rev"]


# ─────────────────────────────────────────────────────────────────────────────
# (f) fetch_for consumed-mark semantics — second call returns []
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_for_consumed_mark(tmp_path_factory, monkeypatch):
    """fetch_for marks consumed_at in one UPDATE, so a second fetch_for on
    the same DB returns []."""
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)
    _seed_row(db, run_id="r-f", role_target="any", severity="info",
              message="once")

    py_first = ops.fetch_for("r-f", "any")
    py_second = ops.fetch_for("r-f", "any")

    assert len(py_first) == 1
    assert py_first[0]["message"] == "once"
    assert py_second == []


# ─────────────────────────────────────────────────────────────────────────────
# (g) fetch_for expires_at + consumed_at filters
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_for_expiry_and_consumed_filters(tmp_path_factory, monkeypatch):
    """fetch_for excludes (a) rows whose expires_at <= now and (b) rows
    whose consumed_at IS NOT NULL."""
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)
    now = int(time.time() * 1000)

    # row-fresh: visible
    _seed_row(db, run_id="r-g", role_target="any", severity="info",
              message="row-fresh",
              created_at=now, expires_at=now + 3600 * 1000)
    # row-expired: expires_at in the past → excluded
    _seed_row(db, run_id="r-g", role_target="any", severity="info",
              message="row-expired",
              created_at=now - 7200 * 1000, expires_at=now - 3600 * 1000)
    # row-consumed: consumed_at set → excluded
    _seed_row(db, run_id="r-g", role_target="any", severity="info",
              message="row-consumed",
              created_at=now, expires_at=now + 3600 * 1000,
              consumed_at=now - 1000)

    py_msgs = sorted(row["message"] for row in ops.fetch_for("r-g", "any"))
    assert py_msgs == ["row-fresh"]


# ─────────────────────────────────────────────────────────────────────────────
# (h) float confidence precision
# ─────────────────────────────────────────────────────────────────────────────
def test_float_confidence_precision(tmp_path_factory, monkeypatch):
    """Confidence=0.123456789 round-trips through emit and fetch_for within
    1e-6. SQLite REAL is IEEE 754 double; we still apply the 1e-6
    tolerance as a guard against any future SQLite encoding changes."""
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)

    py_rowid = ops.emit(
        message="py-float", run_id="r-h",
        role_target="any", confidence=0.123456789,
    )

    py_row = _read_row(db, py_rowid)
    assert abs(py_row["confidence"] - 0.123456789) < 1e-6

    py_fetched = ops.fetch_for("r-h", "any")
    assert len(py_fetched) == 1
    assert abs(py_fetched[0]["confidence"] - 0.123456789) < 1e-6
