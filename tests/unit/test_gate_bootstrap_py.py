"""Standalone unit tests for ``mini_ork.gates.gate_bootstrap``.

Replaces the bash-parity gate (against ``lib/gate_bootstrap.sh``) as part
of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer runs ``lib/gate_bootstrap.sh``
in a subprocess — it asserts the port's behaviour directly. The legacy
script-path backward-compat case seeds the ``<root>/gates/<name>.sh``
condition rows directly via SQL (the exact rows the bash bootstrap used to
write) instead of executing the bash bootstrap.

Contract pinned here: cold bootstrap registers the 5 stable oracle-* ids
with ``native:<name>`` sentinel conditions, warm bootstrap is idempotent,
task_class_filter is SQL NULL, the safety distribution is 4×safety=1 +
1×safety=0 (stability), fail-open rc=0 on missing db/root, and both
native-sentinel and legacy script-path conditions evaluate through the
native evaluators without executing any gate-condition bash script.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.gates import gate_bootstrap as gb  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


EXPECTED_STABLE_IDS = {
    "oracle-coalition",
    "oracle-panel-health",
    "oracle-synthesis-promote",
    "oracle-stability",
    "oracle-liveness",
}


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return dbp


def _row_dump(db: str) -> list[tuple]:
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT gate_id, gate_type, condition, "
        "       CASE WHEN task_class_filter IS NULL THEN '' ELSE task_class_filter END, "
        "       safety, active, registered_at "
        "FROM gate_registry WHERE gate_id LIKE 'oracle-%' "
        "ORDER BY gate_id"
    ).fetchall()
    con.close()
    return [tuple(r) for r in rows]


def _shape_dump(db: str) -> list[tuple]:
    """Row shape excluding registered_at (process-clock-dependent)."""
    return [r[:-1] for r in _row_dump(db)]


def _dump_hash(db: str) -> str:
    canon = "\n".join(repr(r) for r in _shape_dump(db))
    return hashlib.sha256(canon.encode()).hexdigest()


def test_cold_registers_five_oracle_gates(db):
    rc = gb.bootstrap_oracle_gates(db=db, root=str(REPO))
    assert rc == 0
    ids = {r[0] for r in _row_dump(db)}
    assert ids == EXPECTED_STABLE_IDS


def test_warm_idempotent(db):
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    h1 = _dump_hash(db)
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    h2 = _dump_hash(db)
    assert h1 == h2
    assert len(_row_dump(db)) == 5


def test_native_condition_shape_and_registered_at(db):
    """Post-bootstrap rows carry native:<name> sentinel conditions (the
    WS4 contract) and a sane registered_at clock value."""
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    rows = _row_dump(db)
    assert len(rows) == 5
    for r in rows:
        assert r[2].startswith("native:"), r
    assert {r[2] for r in rows} == {
        "native:coalition", "native:panel-health", "native:synthesis-promote",
        "native:stability", "native:liveness",
    }
    now = int(time.time())
    for r in rows:
        assert now - 60 <= r[-1] <= now + 1, f"registered_at drift: {r}"


def test_native_conditions_evaluate_without_bash(db, tmp_path):
    """End-to-end: python bootstrap rows evaluate through the native
    evaluators — no gate-condition bash script is executed. A single-node
    context fail-opens (coalition: single_agent_run → pass; liveness:
    unknown run → PROCEED; stability/panel-health/synthesis-promote:
    missing inputs → defer), so no safety violation may fire."""
    from mini_ork.gates import gate_registry as gr

    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    ctx = ('{"panel_run_id":"run-no-traces","recipe":"code-fix",'
           '"task_class":"code_fix","current_round":1}')
    summary = gr.gate_run_all(db, "code_fix", ctx, mini_ork_root=str(REPO))
    assert summary["gate_count"] == 5
    assert summary["safety_violation"] is False
    verdicts = {g["gate_id"]: g["verdict"] for g in summary["gates"]}
    assert verdicts["oracle-coalition"] == "pass"
    assert verdicts["oracle-liveness"] == "pass"
    assert verdicts["oracle-stability"] == "pass"
    # panel-health + synthesis-promote need verdict_file inputs → defer.
    assert verdicts["oracle-panel-health"] == "defer"
    assert verdicts["oracle-synthesis-promote"] == "defer"


def test_legacy_script_path_conditions_still_evaluate_natively(db):
    """Backward-compat for live DBs: rows with the script-path conditions
    the BASH bootstrap used to register evaluate through the native
    evaluators in the Python registry — the .sh shim is never executed.
    The rows are seeded directly (same shape the bash bootstrap wrote)."""
    from mini_ork.gates import gate_registry as gr

    # Bootstrap first (creates the table + rows), then rewrite the
    # conditions to the legacy script paths the bash bootstrap wrote.
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    names = ("coalition", "panel-health", "synthesis-promote",
             "stability", "liveness")
    con = sqlite3.connect(db)
    for name in names:
        con.execute(
            "UPDATE gate_registry SET condition=? WHERE gate_id=?",
            (f"{REPO}/gates/{name}.sh", f"oracle-{name}"),
        )
    con.commit()
    con.close()

    rows = _row_dump(db)
    assert all(r[2].endswith(".sh") for r in rows)  # legacy bash-seeded paths
    ctx = ('{"panel_run_id":"run-no-traces","recipe":"code-fix",'
           '"task_class":"code_fix","current_round":1}')
    summary = gr.gate_run_all(db, "code_fix", ctx, mini_ork_root=str(REPO))
    assert summary["gate_count"] == 5
    assert summary["safety_violation"] is False
    verdicts = {g["gate_id"]: g["verdict"] for g in summary["gates"]}
    assert verdicts["oracle-coalition"] == "pass"
    assert verdicts["oracle-liveness"] == "pass"


def test_task_class_filter_is_sql_null(db):
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    con = sqlite3.connect(db)
    nulls = con.execute(
        "SELECT COUNT(*) FROM gate_registry "
        "WHERE gate_id LIKE 'oracle-%' AND task_class_filter IS NULL"
    ).fetchone()[0]
    empties = con.execute(
        "SELECT COUNT(*) FROM gate_registry "
        "WHERE gate_id LIKE 'oracle-%' AND task_class_filter = ''"
    ).fetchone()[0]
    con.close()
    assert nulls == 5
    assert empties == 0


def test_safety_distribution(db):
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT gate_id, safety FROM gate_registry WHERE gate_id LIKE 'oracle-%'"
    ).fetchall()
    con.close()
    by_id = {r[0]: r[1] for r in rows}
    assert by_id == {
        "oracle-coalition": 1,
        "oracle-panel-health": 1,
        "oracle-synthesis-promote": 1,
        "oracle-stability": 0,
        "oracle-liveness": 1,
    }
    ones = sum(1 for v in by_id.values() if v == 1)
    zeros = sum(1 for v in by_id.values() if v == 0)
    assert (ones, zeros) == (4, 1)


def test_db_unset_returns_zero_no_file_created(tmp_path):
    rc_py = gb.bootstrap_oracle_gates(db=None, root=str(REPO))
    assert rc_py == 0
    assert not (tmp_path / "state.db").exists()


def test_root_missing_returns_zero(db):
    rc = gb.bootstrap_oracle_gates(db=db, root="")
    assert rc == 0
    try:
        assert _row_dump(db) == []
    except sqlite3.OperationalError:
        pass
    rc2 = gb.bootstrap_oracle_gates(db=db, root="")
    assert rc2 == 0
