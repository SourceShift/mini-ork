"""Parity gate: mini_ork.recovery.circuit_breaker vs lib/circuit_breaker.sh.

Each test drives the LIVE bash function ``mo_check_liveness_breaker`` via
``bash -c 'source lib/circuit_breaker.sh; mo_check_liveness_breaker ...'``
against the SAME SQLite fixture as the Python port, then deep-compares
the two JSON objects: floats within 1e-6, everything else exact-equal.
No mocks, no hardcoded outputs beyond what bash itself emits.

The bash function reads its knobs from the environment
(MO_CB_ARTIFACT_WINDOW / MO_CB_VERDICT_WINDOW / MO_CB_COST_THRESHOLD /
MO_CB_POLICY / MO_CB_COOLDOWN_S / MO_CB_DISABLE); the Python port takes
them as explicit kwargs. ``_compare`` exports the same values to the
bash subprocess that it passes to the Python call, so both sides
honour identical knobs.

Eight cases (above the kickoff's >=6 floor):
  (a) stuck loop → LIVENESS_TRIP / OPEN / fired_count=3 (majority, all fire)
  (b) productive run → PROCEED / CLOSED / fired_count=0 (artifact varies)
  (c) cooldown elapsed → PROCEED, previous_state=HALF_OPEN (probe succeeds)
  (d) unknown run_id → PROCEED, reason=run_unknown_default_proceed,
                       AND the simpler JSON shape (no signals/policy/etc.)
  (e) MO_CB_DISABLE=1 → PROCEED escape hatch (and NO state row written)
  (f) MO_CB_POLICY=or → trips on a single fired signal
  (g) MO_CB_POLICY=and → 2 fired → CLOSED; 3 fired → LIVENESS_TRIP
  (h) circuit_breaker_state row written by bash == row written by python
      (column-by-column; opened_at/updated_at within 60s tolerance)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import circuit_breaker as cb

SH = REPO / "lib" / "circuit_breaker.sh"

_FLOAT_TOL = 1e-6


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (required by lib/circuit_breaker.sh)")
    if not SH.exists():
        pytest.skip(f"missing lib/circuit_breaker.sh at {SH}")


@pytest.fixture
def db(tmp_path_factory):
    """Bootstrap a fresh DB via db/init.sh (full task_runs + execution_traces schema)."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


# ─── seed helpers (raw sqlite3 — full production schema) ──────────────────────


def _now() -> int:
    return int(time.time())


def _seed_task_runs(db: str, rows: list[dict]) -> None:
    """rows = list of {id, task_class, recipe?, artifact_hash?, cost_usd?, created_at}."""
    con = sqlite3.connect(db)
    for r in rows:
        con.execute(
            """
            INSERT INTO task_runs
                (id, task_class, recipe, artifact_hash, cost_usd,
                 kickoff_path, workflow_version, created_at, updated_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'latest', ?, ?, 'classified')
            """,
            (
                r["id"], r["task_class"], r.get("recipe"),
                r.get("artifact_hash"), r.get("cost_usd", 0.0),
                "/tmp/test.kickoff.md",
                r["created_at"], r["created_at"],
            ),
        )
    con.commit()
    con.close()


def _seed_execution_traces(db: str, rows: list[dict]) -> None:
    """rows = list of {trace_id, task_class?, reviewer_verdict?, files_written?, cost_usd?, created_at?}."""
    con = sqlite3.connect(db)
    for r in rows:
        created_at = r.get("created_at")
        if created_at is None:
            # ISO with microsecond precision — matches strftime default format.
            created_at = time.strftime("%Y-%m-%dT%H:%M:%fZ", time.gmtime())
        con.execute(
            """
            INSERT INTO execution_traces
                (trace_id, task_class, status, reviewer_verdict,
                 files_written, cost_usd, duration_ms, created_at)
            VALUES (?, ?, 'success', ?, ?, ?, 0, ?)
            """,
            (
                r["trace_id"], r.get("task_class", "code_fix"),
                r.get("reviewer_verdict"), r.get("files_written", "[]"),
                r.get("cost_usd", 0.0), created_at,
            ),
        )
    con.commit()
    con.close()


def _reset_cb_state(db: str) -> None:
    """Delete all circuit_breaker_state rows (or no-op if the table is absent)."""
    con = sqlite3.connect(db)
    try:
        con.execute("DELETE FROM circuit_breaker_state")
        con.commit()
    except sqlite3.OperationalError:
        pass  # table absent — disable path never created it
    finally:
        con.close()


def _seed_cb_state(db: str, scope_key: str, state: str,
                   opened_at: int | None, trip_count: int = 0) -> None:
    """Pre-populate circuit_breaker_state for cooldown-elapsed fixtures.
    Lazy-creates the table (matches bash _cb_ensure_state_table) so the test
    can seed BEFORE the first call to either side."""
    cb._ensure_state_table(db)
    con = sqlite3.connect(db)
    con.execute(
        """
        INSERT OR REPLACE INTO circuit_breaker_state
            (scope_key, state, opened_at, last_run_id, last_reason,
             trip_count, updated_at)
        VALUES (?, ?, ?, 'seed-run', NULL, ?, ?)
        """,
        (scope_key, state, opened_at, trip_count, _now()),
    )
    con.commit()
    con.close()


def _read_cb_state_row(db: str, scope_key: str) -> dict | None:
    """Read the circuit_breaker_state row for the given scope (None if absent)."""
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "SELECT * FROM circuit_breaker_state WHERE scope_key=?",
            (scope_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        con.close()


def _count_cb_state_rows(db: str) -> int:
    con = sqlite3.connect(db)
    try:
        # Table may not exist if disable path was taken and no prior call ran.
        try:
            (n,) = con.execute(
                "SELECT COUNT(*) FROM circuit_breaker_state"
            ).fetchone()
        except sqlite3.OperationalError:
            n = 0
        return n
    finally:
        con.close()


# ─── parity core ──────────────────────────────────────────────────────────────


def _bash_check(db: str, run_id: str, **knobs) -> tuple[dict, int]:
    """Run LIVE bash mo_check_liveness_breaker; return (parsed JSON, rc)."""
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": db}
    for k, v in knobs.items():
        env_k = f"MO_CB_{k.upper()}"
        if v is not None:
            env[env_k] = str(v)
    src = (
        f'source "{SH}"\n'
        f'mo_check_liveness_breaker "$1"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_", run_id],
        env=env, capture_output=True, text=True,
    )
    line = r.stdout.strip().splitlines()[-1]
    return json.loads(line), r.returncode


def _py_check(db: str, run_id: str, **kwargs) -> tuple[dict, int]:
    """Run the Python port with the same knobs."""
    clean_kwargs = {
        "artifact_window": kwargs.get("artifact_window"),
        "verdict_window": kwargs.get("verdict_window"),
        "cost_threshold": kwargs.get("cost_threshold"),
        "policy": kwargs.get("policy"),
        "cooldown_s": kwargs.get("cooldown_s"),
        "disable": kwargs.get("disable"),
    }
    clean_kwargs = {k: v for k, v in clean_kwargs.items() if v is not None}
    return cb.check_liveness_breaker(run_id, db=db, **clean_kwargs)


def _assert_parity(bash_obj: dict, py_obj: dict, bash_rc: int, py_rc: int) -> None:
    """Deep-compare: same keys; floats within 1e-6; everything else equal."""
    assert bash_rc == py_rc, f"rc mismatch: bash={bash_rc} py={py_rc}"
    assert set(bash_obj.keys()) == set(py_obj.keys()), (
        f"key mismatch\nbash={sorted(bash_obj)}\npy  ={sorted(py_obj)}"
    )
    for k in bash_obj:
        b, p = bash_obj[k], py_obj[k]
        if isinstance(b, bool) or isinstance(p, bool):
            assert b == p, f"key {k!r}: bash={b!r} py={p!r}"
        elif isinstance(b, (int, float)) and isinstance(p, (int, float)):
            assert abs(float(b) - float(p)) <= _FLOAT_TOL, (
                f"key {k!r}: bash={b!r} py={p!r} (diff > {_FLOAT_TOL})"
            )
        elif isinstance(b, dict) and isinstance(p, dict):
            assert set(b.keys()) == set(p.keys()), (
                f"key {k!r} dict key mismatch\nbash={sorted(b)}\npy  ={sorted(p)}"
            )
            for sk in b:
                _assert_scalar(f"{k}.{sk}", b[sk], p[sk])
        elif isinstance(b, list) and isinstance(p, list):
            assert len(b) == len(p), f"key {k!r}: length {len(b)} vs {len(p)}"
            for i, (bi, pi) in enumerate(zip(b, p)):
                _assert_scalar(f"{k}[{i}]", bi, pi)
        else:
            assert b == p, f"key {k!r}: bash={b!r} py={p!r}"


def _assert_scalar(label: str, b, p) -> None:
    if isinstance(b, bool) or isinstance(p, bool):
        assert b == p, f"{label}: bash={b!r} py={p!r}"
    elif isinstance(b, (int, float)) and isinstance(p, (int, float)):
        assert abs(float(b) - float(p)) <= _FLOAT_TOL, (
            f"{label}: bash={b!r} py={p!r} (diff > {_FLOAT_TOL})"
        )
    else:
        assert b == p, f"{label}: bash={b!r} py={p!r}"


def _compare(db: str, run_id: str, *,
             setup_state: Callable[[], None] | None = None, **knobs) -> dict:
    """Run both sides with matching knobs AND matching initial CB state.

    `setup_state` is called BEFORE each side; default behaviour is to wipe
    circuit_breaker_state to empty. Pass a callable for tests that need a
    pre-seeded OPEN row (cooldown-elapsed case).
    """
    def setup():
        _reset_cb_state(db)
        if setup_state is not None:
            setup_state()

    setup()
    bash_obj, bash_rc = _bash_check(db, run_id, **knobs)
    setup()
    py_obj, py_rc = _py_check(db, run_id, **knobs)
    _assert_parity(bash_obj, py_obj, bash_rc, py_rc)
    return bash_obj


# ──────────────────────────────────────────────────────────────────────────────
# (a) Stuck loop → LIVENESS_TRIP / OPEN / fired_count=3 (majority)
#     Mirrors bash self-test fixture A.
# ──────────────────────────────────────────────────────────────────────────────
def test_stuck_loop_trips_parity(db):
    _which_bash()
    now = _now()
    _seed_task_runs(db, [
        {"id": "run-old-1", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "deadbeef" * 4, "cost_usd": 0.5, "created_at": now - 3},
        {"id": "run-old-2", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "deadbeef" * 4, "cost_usd": 0.5, "created_at": now - 2},
        {"id": "run-stuck", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "deadbeef" * 4, "cost_usd": 0.5, "created_at": now - 1},
    ])
    for j in range(3):
        _seed_execution_traces(db, [{
            "trace_id": f"tr-{j}-run-stuck",
            "reviewer_verdict": "REQUEST_CHANGES",
            "files_written": "[]",
            "cost_usd": 0.5,
        }])
    obj = _compare(db, "run-stuck")
    assert obj["verdict"] == "LIVENESS_TRIP"
    assert obj["state"] == "OPEN"
    assert obj["fired_count"] == 3
    assert obj["signals"]["artifact_invariant"]["fired"] is True
    assert obj["signals"]["verdict_stuck"]["fired"] is True
    assert obj["signals"]["cost_burn_no_write"]["fired"] is True


# ──────────────────────────────────────────────────────────────────────────────
# (b) Productive run → PROCEED / CLOSED / fired_count=0
#     Mirrors bash self-test fixture B.
# ──────────────────────────────────────────────────────────────────────────────
def test_productive_run_proceeds_parity(db):
    _which_bash()
    now = _now()
    _seed_task_runs(db, [
        {"id": "run-old-1", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "aaaa" * 4, "cost_usd": 0.3, "created_at": now - 3},
        {"id": "run-old-2", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "bbbb" * 4, "cost_usd": 0.3, "created_at": now - 2},
        {"id": "run-prod", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "cccc" * 4, "cost_usd": 0.3, "created_at": now - 1},
    ])
    for j in range(3):
        _seed_execution_traces(db, [{
            "trace_id": f"tr-{j}-run-prod",
            "reviewer_verdict": "APPROVE",
            "files_written": '[{"path":"src/foo.py","hash":"x"}]',
            "cost_usd": 0.1,
        }])
    obj = _compare(db, "run-prod")
    assert obj["verdict"] == "PROCEED"
    assert obj["state"] == "CLOSED"
    assert obj["fired_count"] == 0
    assert obj["signals"]["artifact_invariant"]["fired"] is False
    assert obj["signals"]["verdict_stuck"]["fired"] is False
    assert obj["signals"]["cost_burn_no_write"]["fired"] is False


# ──────────────────────────────────────────────────────────────────────────────
# (c) Cooldown elapsed → HALF_OPEN probe succeeds → PROCEED
#     Mirrors bash self-test fixture C.
# ──────────────────────────────────────────────────────────────────────────────
def test_cooldown_probe_proceeds_parity(db):
    _which_bash()
    now = _now()
    _seed_task_runs(db, [
        {"id": "run-x", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "1111" * 4, "cost_usd": 0.2, "created_at": now - 3},
        {"id": "run-y", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "2222" * 4, "cost_usd": 0.2, "created_at": now - 2},
        {"id": "run-probe", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "3333" * 4, "cost_usd": 0.2, "created_at": now - 1},
    ])

    def seed_open():
        _seed_cb_state(db, "code_fix::code-fix", "OPEN",
                       opened_at=now - 7200, trip_count=1)

    obj = _compare(db, "run-probe", setup_state=seed_open)
    assert obj["verdict"] == "PROCEED"
    assert obj["previous_state"] == "HALF_OPEN"
    assert obj["state"] == "CLOSED"


# ──────────────────────────────────────────────────────────────────────────────
# (d) Unknown run_id → fail-open PROCEED with simpler JSON shape
#     The key set is a strict subset: no signals/policy/fired_count/
#     cooldown_remaining_s/rationale/remediation.
# ──────────────────────────────────────────────────────────────────────────────
def test_unknown_run_failopen_shape_parity(db):
    _which_bash()
    bash_obj, bash_rc = _bash_check(db, "run-does-not-exist")
    py_obj, py_rc = _py_check(db, "run-does-not-exist")
    _assert_parity(bash_obj, py_obj, bash_rc, py_rc)
    assert bash_obj["verdict"] == "PROCEED"
    assert bash_obj["reason"] == "run_unknown_default_proceed"
    # Verify the simpler shape — keys absent from the normal path.
    assert set(bash_obj.keys()) == {
        "run_id", "state", "verdict", "rationale", "reason",
    }, f"unexpected keys in unknown-run shape: {sorted(bash_obj.keys())}"


# ──────────────────────────────────────────────────────────────────────────────
# (e) MO_CB_DISABLE=1 escape hatch → PROCEED + NO state row written
#     The bash function returns at line 110-112 BEFORE _cb_ensure_state_table,
#     so the disable path must not write a circuit_breaker_state row.
# ──────────────────────────────────────────────────────────────────────────────
def test_disable_escape_hatch_parity(db):
    _which_bash()
    now = _now()
    # Stuck-loop fixture — would normally trip.
    _seed_task_runs(db, [
        {"id": "run-stuck", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "deadbeef" * 4, "cost_usd": 0.5, "created_at": now - 1},
    ])
    for j in range(3):
        _seed_execution_traces(db, [{
            "trace_id": f"tr-{j}-run-stuck",
            "reviewer_verdict": "REQUEST_CHANGES",
            "files_written": "[]",
            "cost_usd": 0.5,
        }])
    bash_obj, bash_rc = _bash_check(db, "run-stuck", disable="1")
    # Pass disable=True explicitly so the Python port matches the env knob
    # the bash subprocess saw.
    py_obj, py_rc = _py_check(db, "run-stuck", disable=True)
    _assert_parity(bash_obj, py_obj, bash_rc, py_rc)
    assert bash_obj["verdict"] == "PROCEED"
    assert "MO_CB_DISABLE=1" in bash_obj["rationale"]
    # Disable path must not write a circuit_breaker_state row.
    assert _count_cb_state_rows(db) == 0, (
        "MO_CB_DISABLE=1 path should not write circuit_breaker_state rows"
    )


# ──────────────────────────────────────────────────────────────────────────────
# (f) MO_CB_POLICY=or → trips on a single fired signal.
#     Fixture: art fires (3 same hash); vd does NOT fire (mixed verdicts
#     including APPROVE); cost does NOT fire (cost < threshold).
#     Under majority this would NOT trip (1 fired < 2); under 'or' it does.
# ──────────────────────────────────────────────────────────────────────────────
def test_policy_or_trips_on_single_signal_parity(db):
    _which_bash()
    now = _now()
    _seed_task_runs(db, [
        {"id": "run-old-1", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "deadbeef" * 4, "cost_usd": 0.1, "created_at": now - 3},
        {"id": "run-old-2", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "deadbeef" * 4, "cost_usd": 0.1, "created_at": now - 2},
        {"id": "run-or", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "deadbeef" * 4, "cost_usd": 0.1, "created_at": now - 1},
    ])
    # Mixed verdicts including APPROVE → verdict_stuck does NOT fire.
    for j, v in enumerate(["APPROVE", "REQUEST_CHANGES", "APPROVE"]):
        _seed_execution_traces(db, [{
            "trace_id": f"tr-{j}-run-or",
            "reviewer_verdict": v,
            "files_written": "[]",
            "cost_usd": 0.1,
        }])
    obj = _compare(db, "run-or", policy="or")
    assert obj["verdict"] == "LIVENESS_TRIP"
    assert obj["state"] == "OPEN"
    assert obj["fired_count"] == 1
    assert obj["policy"] == "or"
    assert obj["signals"]["artifact_invariant"]["fired"] is True
    assert obj["signals"]["verdict_stuck"]["fired"] is False


# ──────────────────────────────────────────────────────────────────────────────
# (g) MO_CB_POLICY=and → 2 fired → CLOSED; 3 fired → LIVENESS_TRIP.
#     Sub-fixture g1: art fires + vd fires + cost does NOT fire (cost below
#     threshold). Under 'and': 2 != 3 → no trip → CLOSED.
#     Sub-fixture g2: same as g1 but cost > threshold + 0 files → cost fires
#     too. Under 'and': 3 == 3 → trip → OPEN.
# ──────────────────────────────────────────────────────────────────────────────
def test_policy_and_requires_all_signals_parity(db):
    _which_bash()
    now = _now()

    def _seed_and_fixture(active_run: str, cost_per_trace: float):
        _seed_task_runs(db, [
            {"id": "run-old-1", "task_class": "code_fix", "recipe": "code-fix",
             "artifact_hash": "deadbeef" * 4, "cost_usd": cost_per_trace,
             "created_at": now - 3},
            {"id": "run-old-2", "task_class": "code_fix", "recipe": "code-fix",
             "artifact_hash": "deadbeef" * 4, "cost_usd": cost_per_trace,
             "created_at": now - 2},
            {"id": active_run, "task_class": "code_fix", "recipe": "code-fix",
             "artifact_hash": "deadbeef" * 4, "cost_usd": cost_per_trace,
             "created_at": now - 1},
        ])
        for j in range(3):
            _seed_execution_traces(db, [{
                "trace_id": f"tr-{j}-{active_run}",
                "reviewer_verdict": "REQUEST_CHANGES",
                "files_written": "[]",
                "cost_usd": cost_per_trace,
            }])

    # g1: 2 fired → CLOSED (art + vd; cost = 0.1 < 1.00 → not fired)
    _seed_and_fixture("run-and-2", cost_per_trace=0.1)
    obj1 = _compare(db, "run-and-2", policy="and")
    assert obj1["verdict"] == "PROCEED"
    assert obj1["state"] == "CLOSED"
    assert obj1["fired_count"] == 2
    assert obj1["policy"] == "and"

    # Reset CB state + traces so the second sub-case has a clean fixture.
    con = sqlite3.connect(db)
    con.execute("DELETE FROM circuit_breaker_state")
    con.execute("DELETE FROM execution_traces")
    con.execute("DELETE FROM task_runs")
    con.commit()
    con.close()

    # g2: 3 fired → LIVENESS_TRIP (cost = 0.5 each, total 1.5 > 1.00)
    _seed_and_fixture("run-and-3", cost_per_trace=0.5)
    obj2 = _compare(db, "run-and-3", policy="and")
    assert obj2["verdict"] == "LIVENESS_TRIP"
    assert obj2["state"] == "OPEN"
    assert obj2["fired_count"] == 3
    assert obj2["policy"] == "and"


# ──────────────────────────────────────────────────────────────────────────────
# (h) circuit_breaker_state row written by bash == row written by python.
#     Run each side against a fresh fixture; diff the resulting row columns.
#     opened_at and updated_at differ by at most a few seconds (real time);
#     the rest must be exact-equal.
# ──────────────────────────────────────────────────────────────────────────────
def test_circuit_breaker_state_row_parity(db):
    _which_bash()
    scope = "code_fix::code-fix"
    ts_window = 60  # seconds — generous real-time tolerance

    def _seed_and_run(side: str) -> dict:
        now = _now()
        _seed_task_runs(db, [
            {"id": "run-old-1", "task_class": "code_fix", "recipe": "code-fix",
             "artifact_hash": "deadbeef" * 4, "cost_usd": 0.5,
             "created_at": now - 3},
            {"id": "run-old-2", "task_class": "code_fix", "recipe": "code-fix",
             "artifact_hash": "deadbeef" * 4, "cost_usd": 0.5,
             "created_at": now - 2},
            {"id": "run-stuck", "task_class": "code_fix", "recipe": "code-fix",
             "artifact_hash": "deadbeef" * 4, "cost_usd": 0.5,
             "created_at": now - 1},
        ])
        for j in range(3):
            _seed_execution_traces(db, [{
                "trace_id": f"tr-{j}-run-stuck",
                "reviewer_verdict": "REQUEST_CHANGES",
                "files_written": "[]",
                "cost_usd": 0.5,
            }])
        if side == "bash":
            _bash_check(db, "run-stuck")
        else:
            _py_check(db, "run-stuck")
        row = _read_cb_state_row(db, scope)
        assert row is not None, f"{side} side did not write a circuit_breaker_state row"
        # Wipe so the next side starts from the same baseline fixture.
        con = sqlite3.connect(db)
        con.execute("DELETE FROM circuit_breaker_state")
        con.execute("DELETE FROM execution_traces")
        con.execute("DELETE FROM task_runs")
        con.commit()
        con.close()
        return row

    bash_row = _seed_and_run("bash")
    py_row = _seed_and_run("py")

    # Same column set.
    assert set(bash_row.keys()) == set(py_row.keys()), (
        f"column mismatch\nbash={sorted(bash_row)}\npy  ={sorted(py_row)}"
    )
    # Exact-equal columns (everything except int timestamps).
    for col in ("scope_key", "state", "last_run_id", "last_reason", "trip_count"):
        assert bash_row[col] == py_row[col], (
            f"column {col!r} mismatch: bash={bash_row[col]!r} py={py_row[col]!r}"
        )
    # Int timestamps — both within ts_window of each other.
    for col in ("opened_at", "updated_at"):
        b, p = bash_row[col], py_row[col]
        assert isinstance(b, int) and isinstance(p, int), (
            f"column {col!r} should be int: bash={type(b).__name__} py={type(p).__name__}"
        )
        assert abs(b - p) <= ts_window, (
            f"column {col!r} drifted: bash={b} py={p} (diff > {ts_window}s)"
        )