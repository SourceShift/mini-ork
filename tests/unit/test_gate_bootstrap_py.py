"""Parity gate: mini_ork.ported.gate_bootstrap vs lib/gate_bootstrap.sh.

Live bash invocation via subprocess + direct Python import. Compares the final
row content of the gate_registry table on fresh temp DBs seeded via db/init.sh.
Row-set equality via sha256 of the sorted row dump — UUID gate_ids generated
during the bash intermediate steps are non-deterministic across processes, so
the comparison contract is the post-rename oracle-* rows only.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import gate_bootstrap as gb  # noqa: E402

GB_SH = REPO / "lib" / "gate_bootstrap.sh"


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
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
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
    """Row shape excluding registered_at (process-clock-dependent across
    subprocess boundaries; the load-bearing contract is id/type/cond/tcf/safety/active)."""
    return [r[:-1] for r in _row_dump(db)]


def _dump_hash(db: str) -> str:
    canon = "\n".join(repr(r) for r in _shape_dump(db))
    return hashlib.sha256(canon.encode()).hexdigest()


def _bash_bootstrap(db: str, *, root: str | None = None,
                    extra_env: dict | None = None) -> int:
    env = {**os.environ, "MINI_ORK_DB": db}
    if root is not None:
        env["MINI_ORK_ROOT"] = root
    if extra_env:
        env.update(extra_env)
    env.pop("MINI_ORK_ROOT", None) if root is None else None
    cmd = f'. "{GB_SH}" && mo_bootstrap_oracle_gates; echo "RC=$?"'
    r = subprocess.run(["bash", "-c", cmd], env=env,
                       capture_output=True, text=True)
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    try:
        return int(last.split("=", 1)[1])
    except (IndexError, ValueError):
        return r.returncode


def test_cold_bash_registers_five_oracle_gates(db):
    rc = _bash_bootstrap(db, root=str(REPO))
    assert rc == 0
    ids = {r[0] for r in _row_dump(db)}
    assert ids == EXPECTED_STABLE_IDS


def test_cold_python_registers_five_oracle_gates(db):
    rc = gb.bootstrap_oracle_gates(db=db, root=str(REPO))
    assert rc == 0
    ids = {r[0] for r in _row_dump(db)}
    assert ids == EXPECTED_STABLE_IDS


def test_warm_bash_idempotent(db):
    assert _bash_bootstrap(db, root=str(REPO)) == 0
    h1 = _dump_hash(db)
    assert _bash_bootstrap(db, root=str(REPO)) == 0
    h2 = _dump_hash(db)
    assert h1 == h2
    assert len(_row_dump(db)) == 5


def test_warm_python_idempotent(db):
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    h1 = _dump_hash(db)
    assert gb.bootstrap_oracle_gates(db=db, root=str(REPO)) == 0
    h2 = _dump_hash(db)
    assert h1 == h2
    assert len(_row_dump(db)) == 5


def test_bash_vs_python_row_parity(db):
    db_bash = db
    db_py = db + ".py"
    import shutil
    shutil.copyfile(db_bash, db_py)
    assert _bash_bootstrap(db_bash, root=str(REPO)) == 0
    assert gb.bootstrap_oracle_gates(db=db_py, root=str(REPO)) == 0
    h_bash = _dump_hash(db_bash)
    h_py = _dump_hash(db_py)
    assert h_bash == h_py, (h_bash, h_py, _row_dump(db_bash), _row_dump(db_py))
    now = int(time.time())
    for r in _row_dump(db_bash) + _row_dump(db_py):
        assert now - 60 <= r[-1] <= now + 1, f"registered_at drift: {r}"


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
    target = tmp_path / "must_not_appear.db"
    env = {k: v for k, v in os.environ.items()
           if k not in {"MINI_ORK_DB", "MINI_ORK_ROOT"}}
    r = subprocess.run(
        ["bash", "-c",
         f'. "{GB_SH}" && mo_bootstrap_oracle_gates; echo "RC=$?"'],
        env=env, capture_output=True, text=True,
    )
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "RC=-1"
    assert last.startswith("RC=0")
    assert not target.exists()

    rc_py = gb.bootstrap_oracle_gates(db=None, root=str(REPO))
    assert rc_py == 0
    assert not (tmp_path / "state.db").exists()


def test_registry_sh_missing_bash_returns_zero(db, tmp_path):
    """Bash fail-open: when lib/gate_registry.sh can't be sourced (root points
    to a dir that lacks it), the function returns 0 without writing anything."""
    fake_root = tmp_path / "fake_root"
    fake_root.mkdir()
    rc = _bash_bootstrap(db, root=str(fake_root))
    assert rc == 0
    try:
        rows = _row_dump(db)
        assert rows == []
    except sqlite3.OperationalError:
        pass


def test_root_missing_python_returns_zero(db):
    rc = gb.bootstrap_oracle_gates(db=db, root="")
    assert rc == 0
    try:
        assert _row_dump(db) == []
    except sqlite3.OperationalError:
        pass
    rc2 = gb.bootstrap_oracle_gates(db=db, root="")
    assert rc2 == 0