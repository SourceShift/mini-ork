"""Unit tests: mini_ork.registries.version_registry (bash parity halves removed; formerly vs lib/version_registry.sh).

Operations through the Python port on a fresh DB; resulting version_registry
rows + return values are asserted semantically. The schema is self-created by
the functions (_ver_ensure_table), so no db/init.sh is needed.
Non-determinism (uuid version_id, time.time() columns) is handled: DB-state
tests pass explicit version_ids and the epoch-second columns are checked for
null-vs-set pattern only; the uuid-minting path is checked structurally.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.registries import version_registry as vr

_TIME_COLS = {"created_at", "promoted_at", "quarantined_at"}


@pytest.fixture
def db(tmp_path):
    # an empty DB file — the functions self-create the table
    return str(tmp_path / "py.db")


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


def test_register_explicit_id(db):
    payload = json.dumps({"name": "code-fix", "version_id": "v-wor-fixed01",
                          "version": "0.1.0", "utility_score": 0.5})
    out_p = vr.register("workflow", payload, db=db)
    assert out_p == "v-wor-fixed01"
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["version_id"] == "v-wor-fixed01"
    assert row["name"] == "code-fix"
    assert abs(float(row["utility_score"]) - 0.5) < 1e-6


def test_register_uuid_path_structure(db):
    payload = json.dumps({"name": "agent-x", "version": "1.0"})
    out_p = vr.register("agent", payload, db=db)
    pat = re.compile(r"^v-age-[0-9a-f]{12}$")
    assert pat.match(out_p), f"uuid-minted id shape drift: {out_p!r}"


def test_register_bad_json_fails(db):
    with pytest.raises(ValueError, match="invalid JSON"):
        vr.register("workflow", "{not json", db=db)


def test_register_missing_name_fails(db):
    with pytest.raises(ValueError, match="must include 'name'"):
        vr.register("workflow", '{"version":"1"}', db=db)


def test_get(db):
    payload = json.dumps({"name": "n1", "version_id": "v-wor-get001", "version": "1"})
    vr.register("workflow", payload, db=db)
    out_p = vr.get("workflow", "v-wor-get001", db=db)
    jp = json.loads(out_p)
    assert jp["version_id"] == "v-wor-get001"
    assert jp["name"] == "n1"
    # missing → "null"
    assert vr.get("workflow", "nope", db=db) == "null"


def test_current(db):
    payload = json.dumps({"name": "svc", "version_id": "v-wor-cur001",
                          "version": "1", "status": "stable"})
    vr.register("workflow", payload, db=db)
    op = json.loads(vr.current("workflow", "svc", db=db))
    assert op["version_id"] == "v-wor-cur001"
    assert op["status"] == "stable"
    # no stable → null
    assert vr.current("workflow", "absent", db=db) == "null"


def test_quarantine_and_can_promote(db):
    payload = json.dumps({"name": "n", "version_id": "v-wor-q0001", "version": "1"})
    vr.register("workflow", payload, db=db)
    # can_promote true before quarantine
    assert vr.can_promote("workflow", "v-wor-q0001", db=db) == "true"
    # unknown version → false
    assert vr.can_promote("workflow", "ghost", db=db) == "false"
    vr.quarantine("workflow", "v-wor-q0001", "flaky", db=db)
    assert vr.can_promote("workflow", "v-wor-q0001", db=db) == "false"
    rows = _rows(db)
    assert rows[0]["status"] == "quarantined"
    assert rows[0]["quarantined_at"] is not None


def test_clear_quarantine(db):
    payload = json.dumps({"name": "n", "version_id": "v-wor-cq001", "version": "1"})
    vr.register("workflow", payload, db=db)
    vr.quarantine("workflow", "v-wor-cq001", "r", db=db)
    vr.clear_quarantine("v-wor-cq001", "alice", db=db)
    rows = _rows(db)
    assert rows[0]["status"] != "quarantined"
    # clearing a non-quarantined version → ValueError
    with pytest.raises(ValueError, match="not found or not quarantined"):
        vr.clear_quarantine("v-wor-cq001", "bob", db=db)


def test_rollback(db):
    v1 = json.dumps({"name": "svc", "version_id": "v-wor-r001", "version": "1", "status": "stable"})
    v2 = json.dumps({"name": "svc", "version_id": "v-wor-r002", "version": "2", "status": "stable"})
    vr.register("workflow", v1, db=db)
    vr.register("workflow", v2, db=db)
    # register() leaves promoted_at NULL, so "current stable" among stable
    # rows is ambiguous; pin it so v2 is current with v1 as its
    # previous_stable_version → rollback retires v2, promotes v1.
    _sql(db, "UPDATE version_registry SET promoted_at=100 WHERE version_id='v-wor-r001'")
    _sql(db, "UPDATE version_registry SET promoted_at=200 WHERE version_id='v-wor-r002'")
    out_p = json.loads(vr.rollback("workflow", "svc", db=db))
    assert out_p["version_id"] == "v-wor-r001"
    rows = {r["version_id"]: r for r in _rows(db)}
    assert rows["v-wor-r002"]["status"] == "retired"
    assert rows["v-wor-r001"]["promoted_at"] is not None
    # rollback with no stable → ValueError
    with pytest.raises(ValueError):
        vr.rollback("workflow", "ghost", db=db)
