"""Hash-chained audit_log writer (mini_ork.stores.audit_chain) and its call sites.

Hermetic: a fresh migrated state.db per test; no provider, no network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.registries import version_registry
from mini_ork.stores import audit_chain
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    rc, _, err = mig.init_db(db=str(path), root=str(REPO))
    assert rc == 0, err
    return str(path)


def _rows(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT audit_id, event_type, actor, target, payload FROM audit_log ORDER BY audit_id"
        ).fetchall()
    finally:
        con.close()


def test_appended_rows_link_into_a_verifiable_chain(db):
    first = audit_chain.record(db, "dispatch", "lane:glm", "model:GLM-5.3", {"status": "success"})
    second = audit_chain.record(db, "gate_verdict", "gate:typecheck", "run:r1", {"verdict": "pass"})

    rows = _rows(db)
    assert [r[1] for r in rows] == ["dispatch", "gate_verdict"]
    p1, p2 = (json.loads(r[4]) for r in rows)
    assert p1["prev_hash"] == audit_chain.GENESIS
    assert p2["prev_hash"] == first["row_hash"] == p1["row_hash"]
    result = audit_chain.verify_chain(db)
    assert result == {"ok": True, "rows": 2, "chained_rows": 2, "head": second["row_hash"],
                      "first_break": None, "reason": ""}


def test_an_edit_made_around_the_triggers_breaks_the_chain(db):
    audit_chain.record(db, "promotion", "version_registry", "workflow:a", {"version_id": "v1"})
    audit_chain.record(db, "promotion", "version_registry", "workflow:b", {"version_id": "v2"})
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER IF EXISTS trg_al_no_update")
    tampered = json.loads(con.execute("SELECT payload FROM audit_log WHERE audit_id=1").fetchone()[0])
    tampered["event"]["version_id"] = "v-evil"
    con.execute("UPDATE audit_log SET payload=? WHERE audit_id=1", (json.dumps(tampered),))
    con.commit()
    con.close()

    result = audit_chain.verify_chain(db)
    assert result["ok"] is False
    assert result["first_break"] == 1
    assert "does not match its hash" in result["reason"]


def test_a_row_inserted_around_the_writer_breaks_the_chain(db):
    audit_chain.record(db, "dispatch", "lane:glm", "model:x", {"status": "success"})
    con = sqlite3.connect(db)
    con.execute("INSERT INTO audit_log (event_type, actor, target, payload) VALUES "
                "('promotion','someone','workflow:a','{}')")
    con.commit()
    con.close()
    audit_chain.record(db, "dispatch", "lane:glm", "model:x", {"status": "success"})

    result = audit_chain.verify_chain(db)
    assert result["ok"] is False
    assert result["first_break"] == 2
    assert "unchained row" in result["reason"]


def test_rows_from_before_the_chain_are_an_unchained_prefix(db):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO audit_log (event_type, actor, target, payload) VALUES "
                "('legacy','old','x','{}')")
    con.commit()
    con.close()
    audit_chain.record(db, "dispatch", "lane:glm", "model:x", {"status": "success"})

    result = audit_chain.verify_chain(db)
    assert result["ok"] is True
    assert (result["rows"], result["chained_rows"]) == (2, 1)


def test_an_unmigrated_database_is_skipped_not_failed(tmp_path):
    con = sqlite3.connect(tmp_path / "bare.db")
    assert audit_chain.append_event(con, "dispatch", "a", "b", {}) is None
    con.close()


def test_version_registry_writes_promotion_quarantine_override_and_rollback(db):
    version_registry.register("workflow", json.dumps({
        "name": "wf", "version_id": "v1", "status": "stable", "baseline_content": "old"}), db=db)
    version_registry.register("workflow", json.dumps({
        "name": "wf", "version_id": "v2", "status": "stable"}), db=db, now=2_000_000_000)
    version_registry.quarantine("workflow", "v2", "regressed", db=db)
    version_registry.clear_quarantine("v2", "amir", db=db)
    version_registry.rollback("workflow", "wf", db=db)

    kinds = [r[1] for r in _rows(db)]
    assert kinds == ["promotion", "promotion", "quarantine", "human_override", "rollback"]
    override = [r for r in _rows(db) if r[1] == "human_override"][0]
    assert override[2] == "human:amir"
    assert audit_chain.verify_chain(db)["ok"] is True


def test_every_llm_calls_row_is_chained_with_its_cost(db, monkeypatch):
    from mini_ork.dispatch.llm_dispatch import write_llm_calls_row

    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-audit-1")
    write_llm_calls_row(db, "gateway", "GLM-5.3", "default", "mini-ork:reviewer", "reviewer",
                        "success", 1200, "0.0123", "", 10, 5, "{}")
    rows = _rows(db)
    assert [r[1] for r in rows] == ["dispatch"]
    event = json.loads(rows[0][4])["event"]
    assert event["status"] == "success"
    assert event["cost_usd"] == pytest.approx(0.0123)
    assert event["run_id"] == "run-audit-1"
    con = sqlite3.connect(db)
    llm_id = con.execute("SELECT max(rowid) FROM llm_calls").fetchone()[0]
    con.close()
    assert event["llm_call_id"] == llm_id
    assert audit_chain.verify_chain(db)["ok"] is True
