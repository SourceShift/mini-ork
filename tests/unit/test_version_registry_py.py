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
    vr.register("workflow", v1, db=db, now=100)
    vr.register("workflow", v2, db=db, now=200)
    # No manual pinning: register() now stamps promoted_at on a stable row, so
    # "which of the stable rows for this name is live" is decided by the
    # ORDER BY instead of by a tie.
    out_p = json.loads(vr.rollback("workflow", "svc", db=db))
    assert out_p["version_id"] == "v-wor-r001"
    rows = {r["version_id"]: r for r in _rows(db)}
    assert rows["v-wor-r002"]["status"] == "retired"
    assert rows["v-wor-r001"]["promoted_at"] is not None
    # rollback with no stable → ValueError
    with pytest.raises(ValueError):
        vr.rollback("workflow", "ghost", db=db)


def test_register_sets_promoted_at_only_for_stable(db):
    """A stable row records when it was promoted; a candidate records nothing.

    ``current()`` and ``rollback()`` pick among rows with ``status='stable'``
    via ``ORDER BY promoted_at DESC``. A name can legitimately hold more than
    one — ``register()`` never demotes the outgoing row — so a NULL here is a
    tie, and a tie makes "which version is live" nondeterministic.
    """
    vr.register("workflow", json.dumps(
        {"name": "s", "version_id": "v-wor-pa01", "status": "stable"}), db=db)
    vr.register("workflow", json.dumps(
        {"name": "s", "version_id": "v-wor-pa02", "status": "candidate"}), db=db)
    rows = {r["version_id"]: r for r in _rows(db)}
    assert rows["v-wor-pa01"]["promoted_at"] is not None
    assert rows["v-wor-pa02"]["promoted_at"] is None


def test_first_promotion_mints_a_baseline_row(db):
    """The first promotion gets a predecessor, so rollback() has somewhere to go.

    An applied prompt mutation is named by its absolute target path and is
    promoted on the *first* apply. Before the baseline row, every such row
    carried ``previous_stable_version = NULL`` and ``rollback()`` raised for all
    of them — the code path existed and had never been able to run.
    """
    vid = vr.register("agent", json.dumps({
        "name": "/repo/p.md", "version_id": "v-age-b0001", "status": "stable",
        "target_path": "/repo/p.md", "content": "AFTER",
        "baseline_content": "BEFORE"}), db=db)
    rows = {r["version_id"]: r for r in _rows(db)}
    promoted = rows[vid]
    assert promoted["previous_stable_version"] is not None
    base = rows[promoted["previous_stable_version"]]
    assert base["status"] == "stable"
    assert json.loads(base["payload"])["content"] == "BEFORE"
    assert base["promoted_at"] < promoted["promoted_at"]
    assert json.loads(vr.current("agent", "/repo/p.md", db=db))["version_id"] == vid


def test_no_baseline_row_without_baseline_content(db):
    """Callers that pass no pre-mutation text get exactly the old behaviour.

    The baseline row is only minted when the caller can supply the prior state;
    a workflow promotion with no such text must not grow a phantom predecessor
    carrying empty content.
    """
    vid = vr.register("workflow", json.dumps({
        "name": "wf", "version_id": "v-wor-nb001", "status": "stable"}), db=db)
    rows = _rows(db)
    assert [r["version_id"] for r in rows] == [vid]
    assert rows[0]["previous_stable_version"] is None


def test_rollback_restores_the_target_file(db, tmp_path, monkeypatch):
    """Rollback writes the pre-mutation bytes back, not just status columns.

    Setting ``status='retired'`` leaves the promoted directive on disk, so the
    system that just "rolled back" keeps executing the change it retired. This
    is the step that makes the registry a backstop rather than a ledger.
    """
    target = tmp_path / "prompt.md"
    target.write_text("AFTER\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    vr.register("agent", json.dumps({
        "name": str(target), "version_id": "v-age-rf001", "status": "stable",
        "target_path": str(target), "content": "AFTER\n",
        "baseline_content": "BEFORE\n"}), db=db)
    assert target.read_text(encoding="utf-8") == "AFTER\n"
    vr.rollback("agent", str(target), db=db)
    assert target.read_text(encoding="utf-8") == "BEFORE\n"


def test_rollback_refuses_a_target_outside_the_active_root(db, tmp_path, monkeypatch):
    """A row promoted by a different checkout is never written into.

    A long-lived DB accumulates absolute target paths into whatever worktree
    promoted them. Restoring one would silently rewrite a stale checkout rather
    than the one being rolled back, so the file write is skipped — loudly — and
    only the DB state moves.
    """
    other = tmp_path / "other"
    other.mkdir()
    target = other / "prompt.md"
    target.write_text("AFTER\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    vr.register("agent", json.dumps({
        "name": str(target), "version_id": "v-age-out01", "status": "stable",
        "target_path": str(target), "content": "AFTER\n",
        "baseline_content": "BEFORE\n"}), db=db)
    vr.rollback("agent", str(target), db=db)
    assert target.read_text(encoding="utf-8") == "AFTER\n"  # untouched
    rows = {r["version_id"]: r for r in _rows(db)}
    assert rows["v-age-out01"]["status"] == "retired"


def test_targets_for_paths_finds_the_rows_a_run_touched(db, tmp_path):
    """Lookup by target path, so a rollback stops guessing the name "default".

    ``_handle_rollback`` rolled back ``("agent", "default")``; no live row is
    named that, so it was a guaranteed no-op that still reported success.
    """
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    for i, p in enumerate((a, b)):
        vr.register("agent", json.dumps({
            "name": str(p), "version_id": f"v-age-tfp0{i}", "status": "stable",
            "target_path": str(p), "content": "x",
            "baseline_content": "y"}), db=db)
    assert [r["name"] for r in vr.targets_for_paths([str(a)], db=db)] == [str(a)]
    assert vr.targets_for_paths([], db=db) == []
    assert vr.targets_for_paths([str(tmp_path / "absent.md")], db=db) == []
