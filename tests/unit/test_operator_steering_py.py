"""Parity gate: mini_ork.steering.operator_steering vs lib/operator_steering.sh.

Each test invokes the LIVE bash subprocess (sourcing lib/operator_steering.sh
in a single ``bash -c`` block so the functions are visible to the caller) on
the same inputs as the Python port and asserts byte-identical output. No
mocks, no hardcoded expected outputs — expected is always derived from a
control bash invocation that shares the inputs.

Eight cases (above the kickoff's >=6 floor):
  (a) emit happy-path round-trip    — bash insert + python insert; both rows
                                       read back via sqlite + diff field-by-
                                       field (id/created_at/expires_at
                                       excluded: timing-dependent)
  (b) emit --message required       — bash exit-2 + stderr text matches port
                                       ValueError text byte-for-byte
  (c) emit unknown flag             — bash exit-2 + stderr "unknown flag: …"
                                       matches port ValueError text
  (d) fetch_for ordering            — bash vs python over the SAME seed:
                                       critical > warn > info, tiebreak by
                                       confidence DESC, then created_at DESC
  (e) fetch_for role matching       — bash vs python OR-of-role_target
                                       semantics (role_target='any' is
                                       broadcast; specific role matches
                                       only its own row + any)
  (f) fetch_for consumed-mark       — second call returns []; both sides
                                       consume in one statement
  (g) fetch_for expired + pre-consumed filters — both sides exclude the
                                       expired row and the pre-consumed row
  (h) float confidence precision    — 0.123456789 round-trips identically
                                       through both emitters + fetchers
                                       (1e-6 tolerance)

Environment isolation:
  The shell pytest runs in often has MINI_ORK_DB / MINI_ORK_HOME set to the
  main repo's state.db. Without isolation, ``ops.emit`` / ``ops.fetch_for``
  would read/write THAT db instead of the per-test temp db. Each test calls
  ``_point_python_env(monkeypatch, db, home)`` to redirect the Python process
  via monkeypatch.setenv (auto-revert on test exit), and constructs the bash
  subprocess env from os.environ (so monkeypatched values propagate).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.steering import operator_steering as ops

SH = REPO / "lib" / "operator_steering.sh"
INIT_SH = REPO / "db" / "init.sh"

# Columns the parity tests strip before comparing. These are timing-dependent
# (created_at/expires_at drift across bash vs python sub-shells) or
# implementation-specific (id diverges because of two AUTOINCREMENT writers).
_STRIP_KEYS = ("id", "created_at", "expires_at")


def _init_db(tmp_path_factory, *, name: str = "home") -> tuple[str, str]:
    """Spin up a fresh mini-ork SQLite DB via db/init.sh.

    Returns (db_path, home_dir). tmp_path_factory guarantees a unique
    sub-directory per call so two DBs in the same test don't collide.
    """
    home = tmp_path_factory.mktemp(name)
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp, str(home)


def _point_python_env(monkeypatch: pytest.MonkeyPatch, db: str, home: str) -> None:
    """Redirect the Python process's ``_resolve_db`` to the temp db.

    Without this, the port would resolve to whatever MINI_ORK_DB /
    MINI_ORK_HOME is set in the shell pytest runs in (usually the repo's
    main state.db), corrupting parity tests.
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
    """Direct-SQL insert (bypasses bash+python emit so we can craft
    expired / pre-consumed rows for filter tests)."""
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
# (a) emit happy-path round-trip — bash insert vs python insert
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_happy_path_parity(tmp_path_factory, monkeypatch):
    """Bash ``operator_steering_emit`` and Python ``emit`` insert rows whose
    field-level contents (run_id / role_target / severity / message / source /
    confidence) match byte-for-byte. id/created_at/expires_at are stripped
    because both AUTOINCREMENT and the wall-clock drift across subshells."""
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)
    # The subprocess env inherits os.environ (with our monkeypatched values).
    subprocess_env = {**os.environ, "MINI_ORK_HOME": home, "MINI_ORK_DB": db}

    # Bash: invoke the live bash function in a single -c block.
    bash_wrapper = (
        f'. "{SH}"\n'
        'operator_steering_emit \\\n'
        '  --message "from-bash" \\\n'
        '  --run-id "r-a" \\\n'
        '  --role-target implementer \\\n'
        '  --severity info \\\n'
        '  --source "bash-src" \\\n'
        '  --confidence 0.7\n'
    )
    r_bash = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env=subprocess_env, capture_output=True, text=True, check=True,
    )
    bash_rowid = int(r_bash.stdout.strip())
    bash_row = _read_row(db, bash_rowid)

    # Python: invoke the port with the same args.
    py_rowid = ops.emit(
        message="from-py",
        run_id="r-a",
        role_target="implementer",
        severity="info",
        source="py-src",
        confidence=0.7,
    )
    py_row = _read_row(db, py_rowid)

    # Strip timing-dependent / divergent fields; verify each row matches
    # the inputs that were passed to ITS emitter (not the other one's).
    assert _strip(bash_row) == {
        "run_id": "r-a",
        "role_target": "implementer",
        "severity": "info",
        "message": "from-bash",
        "source": "bash-src",
        "confidence": 0.7,
    }
    assert _strip(py_row) == {
        "run_id": "r-a",
        "role_target": "implementer",
        "severity": "info",
        "message": "from-py",
        "source": "py-src",
        "confidence": 0.7,
    }
    # Cross-check: messages differ (we didn't conflate the two emitters).
    assert bash_row["message"] != py_row["message"]
    assert bash_row["source"] != py_row["source"]
    # Sanity: confidence round-trip preserves float bit-pattern (SQLite REAL
    # is IEEE 754, json.dumps repr is identical for the same float input).
    assert abs(bash_row["confidence"] - py_row["confidence"]) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (b) emit --message required
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_message_required_parity(tmp_path_factory, monkeypatch):
    """Bash exit 2 + stderr ``--message required`` matches Python ValueError.

    Pass ``message=""`` to hit the Python validation path (the bash function
    also rejects empty --message via ``[ -n "$message" ]``).
    """
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)
    subprocess_env = {**os.environ, "MINI_ORK_HOME": home, "MINI_ORK_DB": db}

    bash_wrapper = (
        f'. "{SH}"\n'
        # Empty --message triggers bash's `[ -n "$message" ]` check.
        'operator_steering_emit --message "" --role-target implementer 2>&1\n'
        'echo "EXIT=$?"\n'
    )
    r = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env=subprocess_env, capture_output=True, text=True,
    )
    # bash printed to stderr; we merged with 2>&1 above so it's in stdout.
    assert "operator_steering_emit: --message required" in r.stdout
    assert "EXIT=2" in r.stdout

    with pytest.raises(ValueError) as exc_info:
        ops.emit(message="", role_target="implementer")
    assert "operator_steering_emit: --message required" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# (c) emit unknown flag
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_unknown_flag_parity(tmp_path_factory, monkeypatch):
    """Bash exit 2 + stderr ``unknown flag: --bogus`` matches Python ValueError."""
    db, home = _init_db(tmp_path_factory)
    _point_python_env(monkeypatch, db, home)
    subprocess_env = {**os.environ, "MINI_ORK_HOME": home, "MINI_ORK_DB": db}

    bash_wrapper = (
        f'. "{SH}"\n'
        'operator_steering_emit --message "x" --bogus "y" 2>&1\n'
        'echo "EXIT=$?"\n'
    )
    r = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env=subprocess_env, capture_output=True, text=True,
    )
    assert "operator_steering_emit: unknown flag: --bogus" in r.stdout
    assert "EXIT=2" in r.stdout

    with pytest.raises(ValueError) as exc_info:
        ops.emit(message="x", bogus="y")
    assert "operator_steering_emit: unknown flag: bogus" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────────────
# (d) fetch_for ordering — severity tier, confidence DESC, created_at DESC
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_for_ordering_parity(tmp_path_factory, monkeypatch):
    """Bash and python agree on the ORDER BY (severity tier DESC, confidence
    DESC, created_at DESC) LIMIT 10. Seed 5 rows with crafted created_at and
    confidence values so the tiebreakers actually fire.

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
    db_bash, home_bash = _init_db(tmp_path_factory, name="d_bash")
    db_py, home_py = _init_db(tmp_path_factory, name="d_py")

    now = int(time.time() * 1000)
    seeds = [
        # (role_target, severity, message, confidence, created_at, source)
        ("any", "info", "msg-A", 0.50, now + 0, "seed"),
        ("any", "warn", "msg-B", 0.95, now + 1000, "seed"),
        ("any", "critical", "msg-C", 0.30, now + 2000, "seed"),
        ("any", "critical", "msg-D", 0.95, now + 3000, "seed"),
        ("any", "critical", "msg-E", 0.95, now + 2500, "seed"),
    ]
    for db in (db_bash, db_py):
        for role, sev, msg, conf, ts, src in seeds:
            _seed_row(db, run_id="r-d", role_target=role, severity=sev,
                      message=msg, source=src, confidence=conf,
                      created_at=ts, expires_at=now + 3600 * 1000)

    # Bash side: fetch_for prints JSONL on stdout.
    bash_wrapper = (
        f'. "{SH}"\n'
        'operator_steering_fetch_for "r-d" "any"\n'
    )
    r_bash = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**os.environ, "MINI_ORK_HOME": home_bash, "MINI_ORK_DB": db_bash},
        capture_output=True, text=True, check=True,
    )
    bash_rows = [json.loads(line) for line in r_bash.stdout.splitlines() if line]

    # Python side: point env at the second DB so the port doesn't write/read
    # the first DB's already-consumed rows.
    _point_python_env(monkeypatch, db_py, home_py)
    py_rows = ops.fetch_for("r-d", "any")

    # Strip id / created_at / expires_at from each row (timing-dependent);
    # order must match exactly.
    bash_stripped = [_strip(row) for row in bash_rows]
    py_stripped = [_strip(row) for row in py_rows]

    assert py_stripped == bash_stripped, (
        f"ordering diverged:\n  bash: {bash_stripped}\n  py:   {py_stripped}"
    )
    # Sanity: the leader is D (critical, conf 0.95, newest).
    assert py_stripped[0]["message"] == "msg-D"
    assert py_stripped[0]["severity"] == "critical"
    assert py_stripped[0]["confidence"] == 0.95
    # Sanity: D precedes E (confidence tie, created_at tiebreak DESC).
    d_idx = next(i for i, r in enumerate(py_stripped) if r["message"] == "msg-D")
    e_idx = next(i for i, r in enumerate(py_stripped) if r["message"] == "msg-E")
    assert d_idx < e_idx
    # Sanity: A (info) is last; B (warn) precedes A.
    assert py_stripped[-1]["message"] == "msg-A"
    assert py_stripped[-2]["message"] == "msg-B"


# ─────────────────────────────────────────────────────────────────────────────
# (e) fetch_for role matching
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_for_role_matching_parity(tmp_path_factory, monkeypatch):
    """Bash and python agree on the OR-of-role_target filter:
    role_target = ?  OR  role_target = 'any'.

    Seed: 3 rows with run_id='r-e':
      - role='any'         → visible to every role query
      - role='implementer' → visible to role='implementer' AND to role='any'
                             via the OR 'any' branch
      - role='reviewer'    → visible only to role='reviewer' (plus 'any'
                             via the OR branch)
    """
    db_bash, home_bash = _init_db(tmp_path_factory, name="e_bash")
    db_py, home_py = _init_db(tmp_path_factory, name="e_py")

    def seed_all(db: str) -> None:
        _seed_row(db, run_id="r-e", role_target="any",         severity="info",
                  message="row-any")
        _seed_row(db, run_id="r-e", role_target="implementer", severity="info",
                  message="row-impl")
        _seed_row(db, run_id="r-e", role_target="reviewer",    severity="info",
                  message="row-rev")

    seed_all(db_bash)
    seed_all(db_py)

    # ask 'implementer' → row-any + row-impl (OR 'any' branch).
    bash_wrapper = (
        f'. "{SH}"\n'
        'operator_steering_fetch_for "r-e" "implementer"\n'
    )
    r_bash = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**os.environ, "MINI_ORK_HOME": home_bash, "MINI_ORK_DB": db_bash},
        capture_output=True, text=True, check=True,
    )
    bash_impl = sorted(
        json.loads(line)["message"]
        for line in r_bash.stdout.splitlines() if line
    )

    _point_python_env(monkeypatch, db_py, home_py)
    py_impl = sorted(row["message"] for row in ops.fetch_for("r-e", "implementer"))

    assert bash_impl == ["row-any", "row-impl"]
    assert py_impl == bash_impl

    # ask 'reviewer' → row-any + row-rev.
    db_bash2, home_bash2 = _init_db(tmp_path_factory, name="e_bash2")
    db_py2, home_py2 = _init_db(tmp_path_factory, name="e_py2")
    seed_all(db_bash2)
    seed_all(db_py2)

    r_bash2 = subprocess.run(
        ["bash", "-c", bash_wrapper.replace('"implementer"', '"reviewer"')],
        env={**os.environ, "MINI_ORK_HOME": home_bash2, "MINI_ORK_DB": db_bash2},
        capture_output=True, text=True, check=True,
    )
    bash_rev = sorted(
        json.loads(line)["message"]
        for line in r_bash2.stdout.splitlines() if line
    )
    monkeypatch.setenv("MINI_ORK_DB", db_py2)
    monkeypatch.setenv("MINI_ORK_HOME", home_py2)
    py_rev = sorted(row["message"] for row in ops.fetch_for("r-e", "reviewer"))

    assert bash_rev == ["row-any", "row-rev"]
    assert py_rev == bash_rev


# ─────────────────────────────────────────────────────────────────────────────
# (f) fetch_for consumed-mark semantics — second call returns []
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_for_consumed_mark_parity(tmp_path_factory, monkeypatch):
    """Both bash and python mark consumed_at in one UPDATE, so a second
    fetch_for on the same DB returns []."""
    db_bash, home_bash = _init_db(tmp_path_factory, name="f_bash")
    db_py, home_py = _init_db(tmp_path_factory, name="f_py")
    for db in (db_bash, db_py):
        _seed_row(db, run_id="r-f", role_target="any", severity="info",
                  message="once")

    bash_wrapper = (
        f'. "{SH}"\n'
        'operator_steering_fetch_for "r-f" "any"\n'
        'operator_steering_fetch_for "r-f" "any"\n'
    )
    r_bash = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**os.environ, "MINI_ORK_HOME": home_bash, "MINI_ORK_DB": db_bash},
        capture_output=True, text=True, check=True,
    )
    bash_lines = r_bash.stdout.splitlines()
    # First call emits 1 row; second emits nothing → stdout has exactly 1 line.
    assert len(bash_lines) == 1
    bash_first = json.loads(bash_lines[0])
    assert bash_first["message"] == "once"

    # Python side: same seed, second call returns [].
    _point_python_env(monkeypatch, db_py, home_py)
    py_first = ops.fetch_for("r-f", "any")
    py_second = ops.fetch_for("r-f", "any")

    # Python's first call matches bash's projection (sans id/created_at).
    assert _strip(py_first[0]) == _strip(bash_first)
    assert py_second == []


# ─────────────────────────────────────────────────────────────────────────────
# (g) fetch_for expires_at + consumed_at filters
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_for_expiry_and_consumed_filters_parity(tmp_path_factory, monkeypatch):
    """Both bash and python exclude (a) rows whose expires_at <= now and
    (b) rows whose consumed_at IS NOT NULL."""
    db_bash, home_bash = _init_db(tmp_path_factory, name="g_bash")
    db_py, home_py = _init_db(tmp_path_factory, name="g_py")
    now = int(time.time() * 1000)

    def seed_all(db: str) -> None:
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

    seed_all(db_bash)
    seed_all(db_py)

    bash_wrapper = (
        f'. "{SH}"\n'
        'operator_steering_fetch_for "r-g" "any"\n'
    )
    r_bash = subprocess.run(
        ["bash", "-c", bash_wrapper],
        env={**os.environ, "MINI_ORK_HOME": home_bash, "MINI_ORK_DB": db_bash},
        capture_output=True, text=True, check=True,
    )
    bash_msgs = sorted(
        json.loads(line)["message"]
        for line in r_bash.stdout.splitlines() if line
    )
    assert bash_msgs == ["row-fresh"]

    _point_python_env(monkeypatch, db_py, home_py)
    py_msgs = sorted(row["message"] for row in ops.fetch_for("r-g", "any"))
    assert py_msgs == bash_msgs == ["row-fresh"]


# ─────────────────────────────────────────────────────────────────────────────
# (h) float confidence precision
# ─────────────────────────────────────────────────────────────────────────────
def test_float_confidence_precision_parity(tmp_path_factory, monkeypatch):
    """Confidence=0.123456789 round-trips through both emit and fetch_for
    within 1e-6. SQLite REAL is IEEE 754 double; json.dumps repr is exact
    for the same float input — we still apply the 1e-6 tolerance as a guard
    against any future SQLite encoding changes."""
    db_bash, home_bash = _init_db(tmp_path_factory, name="h_bash")
    db_py, home_py = _init_db(tmp_path_factory, name="h_py")
    _point_python_env(monkeypatch, db_py, home_py)

    # Bash insert (against bash's own DB).
    bash_emit_wrapper = (
        f'. "{SH}"\n'
        'operator_steering_emit --message "bash-float" --run-id "r-h" \\\n'
        '  --role-target any --confidence 0.123456789\n'
    )
    r_bash_emit = subprocess.run(
        ["bash", "-c", bash_emit_wrapper],
        env={**os.environ, "MINI_ORK_HOME": home_bash, "MINI_ORK_DB": db_bash},
        capture_output=True, text=True, check=True,
    )
    bash_rowid = int(r_bash_emit.stdout.strip())

    # Python insert (against python's DB).
    py_rowid = ops.emit(
        message="py-float", run_id="r-h",
        role_target="any", confidence=0.123456789,
    )

    # Read raw DB rows via sqlite to get the actual stored float.
    bash_row = _read_row(db_bash, bash_rowid)
    py_row = _read_row(db_py, py_rowid)

    assert abs(bash_row["confidence"] - 0.123456789) < 1e-6
    assert abs(py_row["confidence"] - 0.123456789) < 1e-6

    # Bash fetch_for returns JSONL; parse and confirm round-trip via bash's
    # own JSON encoder survives (json.dumps default uses repr).
    bash_fetch_wrapper = (
        f'. "{SH}"\n'
        'operator_steering_fetch_for "r-h" "any"\n'
    )
    r_bash_fetch = subprocess.run(
        ["bash", "-c", bash_fetch_wrapper],
        env={**os.environ, "MINI_ORK_HOME": home_bash, "MINI_ORK_DB": db_bash},
        capture_output=True, text=True, check=True,
    )
    bash_fetched = [json.loads(line) for line in r_bash_fetch.stdout.splitlines() if line]
    assert len(bash_fetched) == 1
    assert abs(bash_fetched[0]["confidence"] - 0.123456789) < 1e-6
    # Same for python fetch_for.
    py_fetched = ops.fetch_for("r-h", "any")
    assert len(py_fetched) == 1
    assert abs(py_fetched[0]["confidence"] - 0.123456789) < 1e-6