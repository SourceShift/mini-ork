"""Parity gate: mini_ork.ported.version_registry vs lib/version_registry.sh.

Same operations through the LIVE bash functions and the Python port on separate
copies of the DB; resulting version_registry rows + stdout must match. The
schema is self-created by the functions (_ver_ensure_table), so no db/init.sh
is needed. Non-determinism (uuid version_id, time.time() columns) is handled:
DB-state tests pass explicit version_ids and the row comparison excludes the
three epoch-second columns (bash and the port run microseconds apart); the
uuid-minting path is checked structurally.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import version_registry as vr  # noqa: E402

SH = REPO / "lib" / "version_registry.sh"
_TIME_COLS = {"created_at", "promoted_at", "quarantined_at"}


def _bash(db, fn, *args):
    r = subprocess.run(
        ["bash", "-c", f'. "{SH}" && {fn} "$@"', "_", *args],
        env={**os.environ, "MINI_ORK_DB": db, "MINI_ORK_ROOT": str(REPO),
             "_MO_VER_SCHEMA_INIT": "0"},
        capture_output=True, text=True)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


@pytest.fixture
def dbs(tmp_path):
    # two empty DB files — the functions self-create the table
    return str(tmp_path / "bash.db"), str(tmp_path / "py.db")


def _rows(db, sql="SELECT * FROM version_registry ORDER BY version_id"):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql).fetchall()]
    con.close()
    return rows


def _sql(db, stmt):
    con = sqlite3.connect(db)
    con.execute(stmt)
    con.commit()
    con.close()


def _strip_time(rows):
    return [{k: v for k, v in r.items() if k not in _TIME_COLS} for r in rows]


def _timecol_nullness(rows):
    return [{k: (r[k] is None) for k in _TIME_COLS} for r in rows]


def _assert_db_parity(db_bash, db_py):
    rb, rp = _rows(db_bash), _rows(db_py)
    assert _strip_time(rb) == _strip_time(rp)
    # epoch columns: same null-vs-set pattern (values differ by run time)
    assert _timecol_nullness(rb) == _timecol_nullness(rp)


def test_register_parity_explicit_id(dbs):
    db_b, db_p = dbs
    payload = json.dumps({"name": "code-fix", "version_id": "v-wor-fixed01",
                          "version": "0.1.0", "utility_score": 0.5})
    out_b, _, rc = _bash(db_b, "version_register", "workflow", payload)
    out_p = vr.register("workflow", payload, db=db_p)
    assert rc == 0 and out_b == out_p == "v-wor-fixed01"
    _assert_db_parity(db_b, db_p)


def test_register_uuid_path_structure(dbs):
    db_b, db_p = dbs
    payload = json.dumps({"name": "agent-x", "version": "1.0"})
    out_b, _, rc = _bash(db_b, "version_register", "agent", payload)
    out_p = vr.register("agent", payload, db=db_p)
    pat = re.compile(r"^v-age-[0-9a-f]{12}$")
    assert rc == 0 and pat.match(out_b) and pat.match(out_p)  # both mint same shape


def test_register_bad_json_fails(dbs):
    db_b, db_p = dbs
    out_b, err_b, rc = _bash(db_b, "version_register", "workflow", "{not json")
    assert rc == 1 and "invalid JSON" in err_b
    with pytest.raises(ValueError, match="invalid JSON"):
        vr.register("workflow", "{not json", db=db_p)


def test_register_missing_name_fails(dbs):
    db_b, db_p = dbs
    out_b, err_b, rc = _bash(db_b, "version_register", "workflow", '{"version":"1"}')
    assert rc == 1 and "must include 'name'" in err_b
    with pytest.raises(ValueError, match="must include 'name'"):
        vr.register("workflow", '{"version":"1"}', db=db_p)


def test_get_parity(dbs):
    db_b, db_p = dbs
    payload = json.dumps({"name": "n1", "version_id": "v-wor-get001", "version": "1"})
    _bash(db_b, "version_register", "workflow", payload)
    vr.register("workflow", payload, db=db_p)
    out_b, _, _ = _bash(db_b, "version_get", "workflow", "v-wor-get001")
    out_p = vr.get("workflow", "v-wor-get001", db=db_p)
    jb, jp = json.loads(out_b), json.loads(out_p)
    for k in _TIME_COLS:
        jb.pop(k, None); jp.pop(k, None)
    assert jb == jp
    # missing → "null" on both
    assert _bash(db_b, "version_get", "workflow", "nope")[0] == vr.get("workflow", "nope", db=db_p) == "null"


def test_current_parity(dbs):
    db_b, db_p = dbs
    payload = json.dumps({"name": "svc", "version_id": "v-wor-cur001",
                          "version": "1", "status": "stable"})
    _bash(db_b, "version_register", "workflow", payload)
    vr.register("workflow", payload, db=db_p)
    ob = json.loads(_bash(db_b, "version_current", "workflow", "svc")[0])
    op = json.loads(vr.current("workflow", "svc", db=db_p))
    for k in _TIME_COLS:
        ob.pop(k, None); op.pop(k, None)
    assert ob == op
    assert ob["version_id"] == "v-wor-cur001"
    # no stable → null
    assert _bash(db_b, "version_current", "workflow", "absent")[0] == vr.current("workflow", "absent", db=db_p) == "null"


def test_quarantine_and_can_promote_parity(dbs):
    db_b, db_p = dbs
    payload = json.dumps({"name": "n", "version_id": "v-wor-q0001", "version": "1"})
    _bash(db_b, "version_register", "workflow", payload)
    vr.register("workflow", payload, db=db_p)
    # can_promote true before quarantine
    assert _bash(db_b, "version_can_promote", "workflow", "v-wor-q0001")[0] == \
        vr.can_promote("workflow", "v-wor-q0001", db=db_p) == "true"
    # unknown version → false
    assert _bash(db_b, "version_can_promote", "workflow", "ghost")[0] == \
        vr.can_promote("workflow", "ghost", db=db_p) == "false"
    _bash(db_b, "version_quarantine", "workflow", "v-wor-q0001", "flaky")
    vr.quarantine("workflow", "v-wor-q0001", "flaky", db=db_p)
    assert _bash(db_b, "version_can_promote", "workflow", "v-wor-q0001")[0] == \
        vr.can_promote("workflow", "v-wor-q0001", db=db_p) == "false"
    _assert_db_parity(db_b, db_p)


def test_clear_quarantine_parity(dbs):
    db_b, db_p = dbs
    payload = json.dumps({"name": "n", "version_id": "v-wor-cq001", "version": "1"})
    for db, is_bash in ((db_b, True), (db_p, False)):
        if is_bash:
            _bash(db, "version_register", "workflow", payload)
            _bash(db, "version_quarantine", "workflow", "v-wor-cq001", "r")
        else:
            vr.register("workflow", payload, db=db)
            vr.quarantine("workflow", "v-wor-cq001", "r", db=db)
    _bash(db_b, "version_clear_quarantine", "v-wor-cq001", "alice")
    vr.clear_quarantine("v-wor-cq001", "alice", db=db_p)
    _assert_db_parity(db_b, db_p)
    # clearing a non-quarantined version → rc 1 / ValueError on both
    _, err_b, rc = _bash(db_b, "version_clear_quarantine", "v-wor-cq001", "bob")
    assert rc == 1 and "not found or not quarantined" in err_b
    with pytest.raises(ValueError, match="not found or not quarantined"):
        vr.clear_quarantine("v-wor-cq001", "bob", db=db_p)


def test_rollback_parity(dbs):
    db_b, db_p = dbs
    v1 = json.dumps({"name": "svc", "version_id": "v-wor-r001", "version": "1", "status": "stable"})
    v2 = json.dumps({"name": "svc", "version_id": "v-wor-r002", "version": "2", "status": "stable"})
    for db, is_bash in ((db_b, True), (db_p, False)):
        if is_bash:
            _bash(db, "version_register", "workflow", v1)
            _bash(db, "version_register", "workflow", v2)
        else:
            vr.register("workflow", v1, db=db)
            vr.register("workflow", v2, db=db)
        # register() leaves promoted_at NULL, so "current stable" among stable
        # rows is ambiguous; pin it identically on both DBs so v2 is current
        # with v1 as its previous_stable_version → rollback retires v2, promotes v1.
        _sql(db, "UPDATE version_registry SET promoted_at=100 WHERE version_id='v-wor-r001'")
        _sql(db, "UPDATE version_registry SET promoted_at=200 WHERE version_id='v-wor-r002'")
    out_b = json.loads(_bash(db_b, "version_rollback", "workflow", "svc")[0])
    out_p = json.loads(vr.rollback("workflow", "svc", db=db_p))
    for k in _TIME_COLS:
        out_b.pop(k, None); out_p.pop(k, None)
    assert out_b == out_p
    _assert_db_parity(db_b, db_p)
    # rollback with no stable → rc1 / ValueError
    _, err_b, rc = _bash(db_b, "version_rollback", "workflow", "ghost")
    assert rc == 1
    with pytest.raises(ValueError):
        vr.rollback("workflow", "ghost", db=db_p)
