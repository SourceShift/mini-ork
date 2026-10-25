"""ECOSYSTEM §6 · K7 — apply-loop candidate hygiene and the dead audit arm.

Mechanism A — ``pick_candidate`` skips sources already decided
              ``quarantined``/``rejected`` for the same
              ``(task_class, target_name)``.
Mechanism B — ``workflow_candidates.status`` follows the gate decision.
Mechanism C — the metric-anchor arm is gone from ``verifier_audit``.

Hermetic: a fresh SQLite file per test under ``tmp_path``, no network, no
provider credentials, no run-local state.
"""
from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

from mini_ork.cli import apply as apply_mod
from mini_ork.learning import verifier_audit


# ─────────────────────────────────────────────────────────────────────────────
# schema + seed helpers
# ─────────────────────────────────────────────────────────────────────────────

_OTHER_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS workflow_candidates (
        candidate_id              TEXT    PRIMARY KEY,
        base_workflow_version_id  TEXT    NOT NULL,
        mutations                 TEXT    NOT NULL DEFAULT '[]',
        status                    TEXT    NOT NULL DEFAULT 'candidate'
                                  CHECK (status IN ('candidate','shadow','promoted',
                                                    'quarantined','deprecated')),
        benchmark_summary_id      TEXT,
        utility_delta             REAL    NOT NULL DEFAULT 0.0,
        created_by                TEXT    NOT NULL DEFAULT 'evolution_engine',
        created_at                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    );

    CREATE TABLE IF NOT EXISTS pattern_records (
        pattern_id            TEXT    PRIMARY KEY,
        description           TEXT    NOT NULL,
        evidence_trace_ids    TEXT    NOT NULL DEFAULT '[]',
        frequency             INTEGER NOT NULL DEFAULT 1,
        first_seen            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        last_seen             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        output_type           TEXT    NOT NULL
                              CHECK (output_type IN (
                                'adr', 'verifier_addition', 'workflow_change',
                                'prompt_change', 'best_practice_rule'
                              )),
        promoted_to           TEXT,
        status                TEXT    NOT NULL DEFAULT 'observed'
                              CHECK (status IN ('observed','promoted','dismissed'))
    );

    CREATE TABLE IF NOT EXISTS emergent_patterns (
        pattern_id           TEXT PRIMARY KEY,
        cluster_label        TEXT NOT NULL,
        member_item_ids_json TEXT NOT NULL DEFAULT '[]',
        feature_set_json     TEXT NOT NULL DEFAULT '[]',
        strength_score       REAL NOT NULL,
        suggested_meta_adr   TEXT,
        status               TEXT NOT NULL DEFAULT 'proposed'
                           CHECK (status IN ('proposed','approved','rejected','superseded')),
        detected_at          INTEGER NOT NULL,
        resolved_at          INTEGER
    );

    CREATE TABLE IF NOT EXISTS gradient_records (
        gradient_id      TEXT PRIMARY KEY,
        target           TEXT NOT NULL,
        signal           TEXT NOT NULL,
        suggested_change TEXT NOT NULL,
        evidence         TEXT NOT NULL DEFAULT '{}',
        confidence       REAL NOT NULL DEFAULT 0.0
                          CHECK(confidence BETWEEN 0.0 AND 1.0),
        created_at       INTEGER NOT NULL,
        task_class       TEXT
    );

    CREATE TABLE IF NOT EXISTS collapse_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_class  TEXT    NOT NULL,
        step        INTEGER NOT NULL,
        score       REAL    NOT NULL,
        anchor      REAL    NOT NULL,
        directives  INTEGER NOT NULL DEFAULT 0,
        run_id      TEXT,
        created_at  INTEGER NOT NULL
    );
"""


def _make_db(tmp_path, name: str = "apply.db") -> str:
    """A fresh SQLite file with ``apply_attempts`` plus the loop's sources."""
    db = str(tmp_path / name)
    con = sqlite3.connect(db)
    con.executescript(apply_mod._APPLY_ATTEMPTS_DDL)
    con.executescript(_OTHER_SCHEMA_SQL)
    con.commit()
    con.close()
    # ``ensure_tables`` is process-wide guarded; reset it so this fresh DB
    # gets the module's own DDL too.
    apply_mod._SCHEMA_INIT = False
    apply_mod.ensure_tables(db)
    return db


def _seed_gradient(db: str, gradient_id: str, task_class: str, target: str,
                   confidence: float) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO gradient_records "
        "(gradient_id, target, signal, suggested_change, evidence, confidence, "
        " created_at, task_class) VALUES (?,?,?,?,?,?,?,?)",
        (gradient_id, target, f"sig-{gradient_id}",
         f"change for {gradient_id}", "{}", confidence, 1, task_class),
    )
    con.commit()
    con.close()


def _seed_pattern(db: str, pattern_id: str, description: str,
                  frequency: int) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO pattern_records "
        "(pattern_id, description, evidence_trace_ids, frequency, first_seen, "
        " last_seen, output_type, status) VALUES (?,?,?,?,?,?,?,?)",
        (pattern_id, description, "[]", frequency,
         "2026-10-01T00:00:00.000Z", "2026-10-02T00:00:00.000Z",
         "prompt_change", "observed"),
    )
    con.commit()
    con.close()


def _seed_attempt(db: str, attempt_id: str, task_class: str, target_name: str,
                  source_kind: str, source_id: str, decision: str) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO apply_attempts "
        "(attempt_id, task_class, target_kind, target_name, source_kind, "
        " source_id, decision, rationale, dry_run, apply_enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, task_class, "agent_prompt", target_name, source_kind,
         source_id, decision, "test seed", 0, 0,
         "2026-10-25T00:00:00.000Z"),
    )
    con.commit()
    con.close()


def _seed_candidate(db: str, candidate_id: str = "cand-1",
                    status: str = "candidate") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO workflow_candidates "
        "(candidate_id, base_workflow_version_id, mutations, status, "
        " created_by, created_at) VALUES (?,?,?,?,?,?)",
        (candidate_id, "wf-v1", "[]", status, "evolution_engine",
         "2026-10-25T00:00:00.000Z"),
    )
    con.commit()
    con.close()


def _candidate_status(db: str, candidate_id: str) -> str:
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT status FROM workflow_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
    finally:
        con.close()
    assert row is not None, "candidate row vanished"
    return row[0]


def _picked(db: str, task_class: str = "tc", target_kind: str = "agent",
            target_name: str = "agent.reviewer.prompt") -> dict:
    raw = apply_mod.pick_candidate(task_class, target_kind, target_name, db=db)
    assert raw, "pick_candidate returned no candidate at all"
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Mechanism A — pick_candidate skips failed sources
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_source_is_skipped(tmp_path):
    db = _make_db(tmp_path)
    _seed_gradient(db, "g-hi", "tc", "agent.reviewer.prompt", 0.9)
    _seed_gradient(db, "g-lo", "tc", "agent.reviewer.prompt", 0.5)
    _seed_attempt(db, "a-1", "tc", "agent.reviewer.prompt",
                  "gradient_records", "g-hi", "rejected")

    picked = _picked(db)

    assert picked["source_id"] == "g-lo"
    assert picked["source_kind"] == "gradient_records"


def test_failure_for_another_target_does_not_hide_the_source(tmp_path):
    db = _make_db(tmp_path)
    _seed_gradient(db, "g-hi", "tc", "agent.reviewer.prompt", 0.9)
    _seed_gradient(db, "g-lo", "tc", "agent.reviewer.prompt", 0.5)
    _seed_attempt(db, "a-1", "tc", "other",
                  "gradient_records", "g-hi", "rejected")

    picked = _picked(db)

    assert picked["source_id"] == "g-hi"


def test_pattern_records_failed_source_is_skipped(tmp_path):
    db = _make_db(tmp_path)
    _seed_pattern(db, "p-1", "tighten agent.reviewer.prompt wording", 5)
    _seed_pattern(db, "p-2", "tighten agent.reviewer.prompt wording", 3)
    _seed_attempt(db, "a-1", "tc", "agent.reviewer.prompt",
                  "pattern_records", "p-1", "quarantined")

    picked = _picked(db)

    assert picked["source_id"] == "p-2"
    assert picked["source_kind"] == "pattern_records"


def test_previously_failed_defence_in_depth_stays(tmp_path):
    """Kickoff constraint: keep the apply_run check unchanged."""
    db = _make_db(tmp_path)
    _seed_attempt(db, "a-1", "tc", "agent.reviewer.prompt",
                  "gradient_records", "g-hi", "rejected")

    assert apply_mod._previously_failed(
        "tc", "agent.reviewer.prompt", "g-hi", db=db) is True
    assert apply_mod._previously_failed(
        "tc", "agent.reviewer.prompt", "g-lo", db=db) is False


# ─────────────────────────────────────────────────────────────────────────────
# Mechanism B — candidate status follows the gate decision
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("decision,expected", [
    ("promoted", "promoted"),
    ("quarantined", "quarantined"),
    ("rejected", "deprecated"),
    ("no_candidate", "candidate"),
])
def test_status_follows_the_decision(tmp_path, decision, expected):
    db = _make_db(tmp_path)
    _seed_candidate(db, "cand-1", status="candidate")

    apply_mod._set_candidate_status("cand-1", decision, db)

    assert _candidate_status(db, "cand-1") == expected


def test_status_write_swallows_sqlite_errors(tmp_path):
    db = str(tmp_path / "no-candidates.db")
    con = sqlite3.connect(db)
    con.executescript(apply_mod._APPLY_ATTEMPTS_DDL)
    con.commit()
    con.close()

    # Must not raise: the UPDATE targets a table that does not exist here.
    apply_mod._set_candidate_status("cand-1", "promoted", db)


# ─────────────────────────────────────────────────────────────────────────────
# Mechanism C — the metric-anchor arm is gone
# ─────────────────────────────────────────────────────────────────────────────

def test_audit_has_no_metric_anchor_arm(tmp_path):
    db = str(tmp_path / "audit.db")
    con = sqlite3.connect(db)
    con.executescript(_OTHER_SCHEMA_SQL)
    con.commit()
    con.close()

    result = verifier_audit.audit(None, db)

    assert "metric_anchor" not in result["evidence"]
    assert "metric_anchor" not in result["flags"]
    assert "metric_history" not in inspect.signature(
        verifier_audit.audit).parameters
