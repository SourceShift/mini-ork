"""Standalone unit tests for ``mini_ork.recovery.circuit_breaker``.

Replaces the bash-parity gate (against ``lib/circuit_breaker.sh``) as part
of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer drives the LIVE bash function
``mo_check_liveness_breaker`` in a subprocess — it asserts the port's
behaviour directly. The expected values below are the semantic contract
the bash side used to pin (verdicts, states, fired signals, fail-open
shapes, escape hatches, state-row writes), now asserted on the port's
output.

Eight cases:
  (a) stuck loop → LIVENESS_TRIP / OPEN / fired_count=3 (majority, all fire)
  (b) productive run → PROCEED / CLOSED / fired_count=0 (artifact varies)
  (c) cooldown elapsed → PROCEED, previous_state=HALF_OPEN (probe succeeds)
  (d) unknown run_id → PROCEED, reason=run_unknown_default_proceed,
                       AND the simpler JSON shape (no signals/policy/etc.)
  (e) disable=True → PROCEED escape hatch (and NO state row written)
  (f) policy='or' → trips on a single fired signal
  (g) policy='and' → 2 fired → CLOSED; 3 fired → LIVENESS_TRIP
  (h) circuit_breaker_state row contents after a trip
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.recovery import circuit_breaker as cb  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


@pytest.fixture
def db(tmp_path_factory):
    """Bootstrap a fresh DB via init_db (full task_runs + execution_traces schema)."""
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
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
    Lazy-creates the table (matches _cb_ensure_state_table) so the test
    can seed BEFORE the first call."""
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


# ──────────────────────────────────────────────────────────────────────────────
# (a) Stuck loop → LIVENESS_TRIP / OPEN / fired_count=3 (majority)
# ──────────────────────────────────────────────────────────────────────────────
def test_stuck_loop_trips(db):
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
    _reset_cb_state(db)
    obj, rc = cb.check_liveness_breaker("run-stuck", db=db)
    assert rc == 1
    assert obj["verdict"] == "LIVENESS_TRIP"
    assert obj["state"] == "OPEN"
    assert obj["fired_count"] == 3
    assert obj["signals"]["artifact_invariant"]["fired"] is True
    assert obj["signals"]["verdict_stuck"]["fired"] is True
    assert obj["signals"]["cost_burn_no_write"]["fired"] is True


# ──────────────────────────────────────────────────────────────────────────────
# (b) Productive run → PROCEED / CLOSED / fired_count=0
# ──────────────────────────────────────────────────────────────────────────────
def test_productive_run_proceeds(db):
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
    _reset_cb_state(db)
    obj, rc = cb.check_liveness_breaker("run-prod", db=db)
    assert rc == 0
    assert obj["verdict"] == "PROCEED"
    assert obj["state"] == "CLOSED"
    assert obj["fired_count"] == 0
    assert obj["signals"]["artifact_invariant"]["fired"] is False
    assert obj["signals"]["verdict_stuck"]["fired"] is False
    assert obj["signals"]["cost_burn_no_write"]["fired"] is False


# ──────────────────────────────────────────────────────────────────────────────
# (c) Cooldown elapsed → HALF_OPEN probe succeeds → PROCEED
# ──────────────────────────────────────────────────────────────────────────────
def test_cooldown_probe_proceeds(db):
    now = _now()
    _seed_task_runs(db, [
        {"id": "run-x", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "1111" * 4, "cost_usd": 0.2, "created_at": now - 3},
        {"id": "run-y", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "2222" * 4, "cost_usd": 0.2, "created_at": now - 2},
        {"id": "run-probe", "task_class": "code_fix", "recipe": "code-fix",
         "artifact_hash": "3333" * 4, "cost_usd": 0.2, "created_at": now - 1},
    ])

    _reset_cb_state(db)
    _seed_cb_state(db, "code_fix::code-fix", "OPEN",
                   opened_at=now - 7200, trip_count=1)
    obj, rc = cb.check_liveness_breaker("run-probe", db=db)
    assert rc == 0
    assert obj["verdict"] == "PROCEED"
    assert obj["previous_state"] == "HALF_OPEN"
    assert obj["state"] == "CLOSED"


# ──────────────────────────────────────────────────────────────────────────────
# (d) Unknown run_id → fail-open PROCEED with simpler JSON shape
#     The key set is a strict subset: no signals/policy/fired_count/
#     cooldown_remaining_s/rationale/remediation.
# ──────────────────────────────────────────────────────────────────────────────
def test_unknown_run_failopen_shape(db):
    obj, rc = cb.check_liveness_breaker("run-does-not-exist", db=db)
    assert rc == 0
    assert obj["verdict"] == "PROCEED"
    assert obj["reason"] == "run_unknown_default_proceed"
    # Verify the simpler shape — keys absent from the normal path.
    assert set(obj.keys()) == {
        "run_id", "state", "verdict", "rationale", "reason",
    }, f"unexpected keys in unknown-run shape: {sorted(obj.keys())}"


# ──────────────────────────────────────────────────────────────────────────────
# (e) disable=True escape hatch → PROCEED + NO state row written
#     The disable path returns BEFORE the state table is ensured, so it
#     must not write a circuit_breaker_state row.
# ──────────────────────────────────────────────────────────────────────────────
def test_disable_escape_hatch(db):
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
    obj, rc = cb.check_liveness_breaker("run-stuck", db=db, disable=True)
    assert rc == 0
    assert obj["verdict"] == "PROCEED"
    assert "MO_CB_DISABLE=1" in obj["rationale"]
    # Disable path must not write a circuit_breaker_state row.
    assert _count_cb_state_rows(db) == 0, (
        "disable path should not write circuit_breaker_state rows"
    )


# ──────────────────────────────────────────────────────────────────────────────
# (f) policy='or' → trips on a single fired signal.
#     Fixture: art fires (3 same hash); vd does NOT fire (mixed verdicts
#     including APPROVE); cost does NOT fire (cost < threshold).
#     Under majority this would NOT trip (1 fired < 2); under 'or' it does.
# ──────────────────────────────────────────────────────────────────────────────
def test_policy_or_trips_on_single_signal(db):
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
    _reset_cb_state(db)
    obj, rc = cb.check_liveness_breaker("run-or", db=db, policy="or")
    assert rc == 1
    assert obj["verdict"] == "LIVENESS_TRIP"
    assert obj["state"] == "OPEN"
    assert obj["fired_count"] == 1
    assert obj["policy"] == "or"
    assert obj["signals"]["artifact_invariant"]["fired"] is True
    assert obj["signals"]["verdict_stuck"]["fired"] is False


# ──────────────────────────────────────────────────────────────────────────────
# (g) policy='and' → 2 fired → CLOSED; 3 fired → LIVENESS_TRIP.
#     Sub-fixture g1: art fires + vd fires + cost does NOT fire (cost below
#     threshold). Under 'and': 2 != 3 → no trip → CLOSED.
#     Sub-fixture g2: same as g1 but cost > threshold + 0 files → cost fires
#     too. Under 'and': 3 == 3 → trip → OPEN.
# ──────────────────────────────────────────────────────────────────────────────
def test_policy_and_requires_all_signals(db):
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
    _reset_cb_state(db)
    obj1, rc1 = cb.check_liveness_breaker("run-and-2", db=db, policy="and")
    assert rc1 == 0
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
    obj2, rc2 = cb.check_liveness_breaker("run-and-3", db=db, policy="and")
    assert rc2 == 1
    assert obj2["verdict"] == "LIVENESS_TRIP"
    assert obj2["state"] == "OPEN"
    assert obj2["fired_count"] == 3
    assert obj2["policy"] == "and"


# ──────────────────────────────────────────────────────────────────────────────
# (h) circuit_breaker_state row contents after a trip.
# ──────────────────────────────────────────────────────────────────────────────
def test_circuit_breaker_state_row_written(db):
    scope = "code_fix::code-fix"
    ts_window = 60  # seconds — generous real-time tolerance

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
    _reset_cb_state(db)
    cb.check_liveness_breaker("run-stuck", db=db)

    row = _read_cb_state_row(db, scope)
    assert row is not None, "port did not write a circuit_breaker_state row"
    assert row["scope_key"] == scope
    assert row["state"] == "OPEN"
    assert row["last_run_id"] == "run-stuck"
    assert row["trip_count"] == 1
    # Int timestamps — both within ts_window of now.
    for col in ("opened_at", "updated_at"):
        v = row[col]
        assert isinstance(v, int), (
            f"column {col!r} should be int: {type(v).__name__}"
        )
        assert abs(v - now) <= ts_window, (
            f"column {col!r} drifted: {v} vs {now} (diff > {ts_window}s)"
        )
