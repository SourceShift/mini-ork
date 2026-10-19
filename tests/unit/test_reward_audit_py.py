"""Reward/evidence pairing: ``mini_ork.learning.reward_audit`` + the GRPO filter.

A positive GRPO reward is a *claim* that the run succeeded. ``verifier_results``
is the independent measurement. These tests pin the pairing contract:

* classification is ``unmeasured`` / ``backed`` / ``contradicted``, with an
  operator false-positive override and a legacy missing-table degradation;
* ``pair`` only drops positive rewards carrying contradicted evidence;
* ``write_grpo_advantages`` drops those rows before group aggregation (so the
  advantage math changes), memoizes classification per distinct ``run_id``,
  keeps ``unmeasured`` rows in the corpus, and never fails learning when the
  oversight inbox does.

Hermetic: every fixture is a fresh temp SQLite DB built from the DDL of the
migrations this feature reads — ``verifier_results`` (0025), the
``execution_traces`` column set used by the writeback (0010 + 0031 + 0042), and
``agent_performance_memory`` (0009 + the relative_advantage column). No fixture
is imported from another test module and no ``db/init.sh`` subprocess runs.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork import cn_client  # noqa: E402
from mini_ork.gates import oversight_inbox  # noqa: E402
from mini_ork.learning import reward_audit, writeback  # noqa: E402

NOW_ISO = "2026-10-19T12:00:00Z"

# verifier_results — verbatim column set from db/migrations/0025_verifier_rubrics.sql
# (the rubric FK is dropped because the parent table is not in play).
VERIFIER_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS verifier_results (
    result_id            TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    verifier_name        TEXT NOT NULL,
    rubric_id            TEXT,
    verdict              TEXT NOT NULL
                            CHECK(verdict IN ('pass', 'fail', 'indeterminate', 'vacuous')),
    confidence           REAL,
    scored_axes_json     TEXT,
    is_false_positive    INTEGER NOT NULL DEFAULT 0
                            CHECK(is_false_positive IN (0, 1)),
    is_false_negative    INTEGER NOT NULL DEFAULT 0
                            CHECK(is_false_negative IN (0, 1)),
    annotated_by         TEXT,
    annotated_at         INTEGER,
    repair_run_id        TEXT,
    notes                TEXT,
    created_at           INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    CHECK(NOT (is_false_positive = 1 AND is_false_negative = 1))
);
"""

# Every column write_grpo_advantages SELECTs (0010 lineage + 0031 process_reward
# + 0042 validity + the live TEXT run_id).
EXECUTION_TRACES_DDL = """
CREATE TABLE IF NOT EXISTS execution_traces (
    trace_id         TEXT PRIMARY KEY,
    run_id           TEXT,
    task_class       TEXT,
    agent_version_id TEXT,
    verifier_output  TEXT,
    status           TEXT,
    reviewer_verdict TEXT,
    cost_usd         REAL DEFAULT 0.0,
    duration_ms      REAL DEFAULT 0.0,
    process_reward   REAL DEFAULT NULL,
    validity         TEXT DEFAULT 'valid',
    created_at       TEXT
);
"""

# agent_performance_memory — 0009_memory_namespaces.sql + relative_advantage.
APM_DDL = """
CREATE TABLE IF NOT EXISTS agent_performance_memory (
  agent_version_id    TEXT    NOT NULL,
  role                TEXT    NOT NULL,
  model               TEXT    NOT NULL,
  task_class          TEXT    NOT NULL,
  runs_count          INTEGER NOT NULL DEFAULT 0,
  success_count       INTEGER NOT NULL DEFAULT 0,
  avg_cost_usd        REAL    NOT NULL DEFAULT 0.0,
  avg_duration_ms     REAL    NOT NULL DEFAULT 0.0,
  top_failure_modes   TEXT    NOT NULL DEFAULT '[]',
  last_updated        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  relative_advantage  REAL    NOT NULL DEFAULT 0.0,
  PRIMARY KEY (agent_version_id, task_class)
);
"""

TRACE_COLS = (
    "trace_id, run_id, task_class, agent_version_id, verifier_output, status, "
    "reviewer_verdict, cost_usd, duration_ms, process_reward, validity, created_at"
)


def _connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


@pytest.fixture
def make_db(tmp_path):
    """Fresh temp DB per call; the caller asks which tables exist."""
    made = []

    def _make(*, with_verifier_results=True, with_traces=True):
        db = str(tmp_path / f"reward-audit-{len(made)}.db")
        made.append(db)
        con = _connect(db)
        try:
            if with_traces:
                con.execute(EXECUTION_TRACES_DDL)
            con.execute(APM_DDL)
            if with_verifier_results:
                con.execute(VERIFIER_RESULTS_DDL)
            con.commit()
        finally:
            con.close()
        return db

    return _make


def _trace(trace_id, run_id, agent="agent-x", *, status="success", verdict=None,
           task_class="code-fix", cost=0.10, validity="valid"):
    return (trace_id, run_id, task_class, agent,
            '{"node_type": "implementer"}', status, verdict, cost, 1000.0,
            None, validity, NOW_ISO)


def _add_traces(db, traces):
    con = _connect(db)
    try:
        con.executemany(
            f"INSERT INTO execution_traces ({TRACE_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            list(traces),
        )
        con.commit()
    finally:
        con.close()


def _add_verifier(db, result_id, run_id, verdict, *, is_false_positive=0):
    con = _connect(db)
    try:
        con.execute(
            "INSERT INTO verifier_results (result_id, run_id, verifier_name, "
            "verdict, is_false_positive) VALUES (?,?,?,?,?)",
            (result_id, run_id, "spec-verifier", verdict, is_false_positive),
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _pin_learning_env(monkeypatch):
    """Deterministic learning knobs; pairing left at its default (on)."""
    monkeypatch.setenv("MO_LEARNING_DECAY_ALPHA", "0.30")
    monkeypatch.setenv("MO_LEARNING_HALFLIFE_DAYS", "0")
    monkeypatch.setenv("MO_LEARNING_TIEBREAK", "1")
    monkeypatch.setenv("MO_LEARNING_SHRINKAGE_K", "5")
    monkeypatch.delenv("MO_REWARD_EVIDENCE_PAIRING", raising=False)
    monkeypatch.delenv("MO_REWARD_DIVERGENCE_ENQUEUE_LIMIT", raising=False)


@pytest.fixture(autouse=True)
def _no_cn(monkeypatch):
    calls = []

    def _fake_upsert(nodes, edges, source="mini-ork"):
        calls.append({"nodes": list(nodes), "edges": list(edges)})
        return 0

    monkeypatch.setattr(cn_client, "graph_upsert_batched", _fake_upsert)
    return calls


def _apm(db):
    con = _connect(db)
    try:
        rows = con.execute(
            "SELECT agent_version_id, task_class, runs_count, success_count, "
            "relative_advantage FROM agent_performance_memory"
        ).fetchall()
    finally:
        con.close()
    return {
        (r["agent_version_id"], r["task_class"]): (
            int(r["runs_count"]),
            int(r["success_count"]),
            round(float(r["relative_advantage"]), 6),
        )
        for r in rows
    }


def _gate_rows(db):
    con = _connect(db)
    try:
        return con.execute(
            "SELECT * FROM mo_inbox_gates WHERE gate_id = ?",
            (reward_audit.DIVERGENCE_GATE_ID,),
        ).fetchall()
    finally:
        con.close()


def _run_writeback(db, *, knob=None):
    if knob is None:
        os.environ.pop("MO_REWARD_EVIDENCE_PAIRING", None)
    else:
        os.environ["MO_REWARD_EVIDENCE_PAIRING"] = knob
    return writeback.write_grpo_advantages(db)


# ── classify_run ─────────────────────────────────────────────────────────────


def test_classify_run_no_rows_is_unmeasured(make_db):
    db = make_db()
    _add_verifier(db, "vr-1", "run-other", "pass")
    con = _connect(db)
    try:
        assert reward_audit.classify_run(con, "run-alpha") == reward_audit.UNMEASURED
    finally:
        con.close()


def test_classify_run_genuine_pass_is_backed(make_db):
    db = make_db()
    _add_verifier(db, "vr-1", "run-alpha", "pass")
    con = _connect(db)
    try:
        assert reward_audit.classify_run(con, "run-alpha") == reward_audit.BACKED
    finally:
        con.close()


def test_classify_run_only_failures_is_contradicted(make_db):
    db = make_db()
    _add_verifier(db, "vr-1", "run-alpha", "fail")
    _add_verifier(db, "vr-2", "run-alpha", "indeterminate")
    con = _connect(db)
    try:
        assert reward_audit.classify_run(con, "run-alpha") == reward_audit.CONTRADICTED
    finally:
        con.close()


def test_classify_run_false_positive_pass_is_not_a_pass(make_db):
    """Operator ground truth overrides the verdict: a flagged pass is contradicted."""
    db = make_db()
    _add_verifier(db, "vr-1", "run-alpha", "pass", is_false_positive=1)
    con = _connect(db)
    try:
        assert reward_audit.classify_run(con, "run-alpha") == reward_audit.CONTRADICTED
    finally:
        con.close()


def test_classify_run_one_genuine_pass_among_failures_is_backed(make_db):
    db = make_db()
    _add_verifier(db, "vr-1", "run-alpha", "fail")
    _add_verifier(db, "vr-2", "run-alpha", "pass")
    con = _connect(db)
    try:
        assert reward_audit.classify_run(con, "run-alpha") == reward_audit.BACKED
    finally:
        con.close()


def test_classify_run_missing_table_is_unmeasured_and_read_only(make_db):
    db = make_db(with_verifier_results=False)
    con = _connect(db)
    try:
        assert reward_audit.classify_run(con, "run-alpha") == reward_audit.UNMEASURED
    finally:
        con.close()


def test_classify_run_never_writes(make_db):
    db = make_db()
    _add_verifier(db, "vr-1", "run-alpha", "pass")
    con = _connect(db)
    try:
        before = con.total_changes
        for _ in range(3):
            reward_audit.classify_run(con, "run-alpha")
        assert con.total_changes == before
    finally:
        con.close()


# ── pair ─────────────────────────────────────────────────────────────────────


def test_pair_none_passthrough():
    assert reward_audit.pair(None, reward_audit.BACKED) == (None, reward_audit.BACKED)


def test_pair_negative_reward_needs_no_evidence():
    assert reward_audit.pair(0.2, reward_audit.CONTRADICTED) == (0.2, reward_audit.CONTRADICTED)
    assert reward_audit.pair(0.49, reward_audit.CONTRADICTED) == (0.49, reward_audit.CONTRADICTED)


def test_pair_threshold_is_inclusive():
    assert reward_audit.pair(0.5, reward_audit.CONTRADICTED) == (None, reward_audit.CONTRADICTED)


def test_pair_positive_contradicted_drops():
    assert reward_audit.pair(0.85, reward_audit.CONTRADICTED) == (None, reward_audit.CONTRADICTED)
    assert reward_audit.pair(1.0, reward_audit.CONTRADICTED) == (None, reward_audit.CONTRADICTED)


def test_pair_backed_and_unmeasured_keep_reward():
    assert reward_audit.pair(0.85, reward_audit.BACKED) == (0.85, reward_audit.BACKED)
    assert reward_audit.pair(0.85, reward_audit.UNMEASURED) == (0.85, reward_audit.UNMEASURED)


# ── write_grpo_advantages pairing ────────────────────────────────────────────

X_CONTRA = _trace("t-x1", "run-alpha", "agent-x", cost=0.10)
Y_UNMEAS = _trace("t-y1", "run-beta", "agent-y", cost=0.20)


def test_contradicted_positive_row_is_excluded_from_the_group(make_db):
    db = make_db()
    _add_traces(db, [X_CONTRA, Y_UNMEAS])
    _add_verifier(db, "vr-fail", "run-alpha", "fail")

    ret_off = _run_writeback(db, knob="off")
    snap_off = _apm(db)
    assert ret_off == 2
    assert snap_off[("agent-x", "code-fix")][0] == 1
    # cost tie-break drove agent-y's negative advantage in the paired group
    assert snap_off[("agent-y", "code-fix")][2] == pytest.approx(-0.016667, abs=1e-4)

    db2 = make_db()
    _add_traces(db2, [X_CONTRA, Y_UNMEAS])
    _add_verifier(db2, "vr-fail", "run-alpha", "fail")
    ret_on = _run_writeback(db2)
    snap_on = _apm(db2)
    assert ret_on == 1  # group size differs from the unpaired baseline
    assert ("agent-x", "code-fix") not in snap_on
    # alone in the group, the cost span is zero → advantage 0
    assert snap_on[("agent-y", "code-fix")][2] == pytest.approx(0.0, abs=1e-9)
    assert snap_on[("agent-y", "code-fix")][2] != snap_off[("agent-y", "code-fix")][2]


def test_backed_positive_row_stays_in_the_group(make_db):
    db = make_db()
    _add_traces(db, [X_CONTRA, Y_UNMEAS])
    _add_verifier(db, "vr-pass", "run-alpha", "pass")
    ret = _run_writeback(db)
    snap = _apm(db)
    assert ret == 2
    assert ("agent-x", "code-fix") in snap


def test_unmeasured_row_is_never_dropped(make_db):
    """No verifier rows is not evidence of failure; only 'contradicted' drops."""
    db = make_db()
    _add_traces(db, [X_CONTRA, Y_UNMEAS])
    ret = _run_writeback(db)
    snap = _apm(db)
    assert ret == 2
    assert set(snap) == {("agent-x", "code-fix"), ("agent-y", "code-fix")}


def test_off_and_zero_restore_today_exactly(make_db):
    # Today's behavior, computed on a legacy DB with no verifier_results table.
    db_today = make_db(with_verifier_results=False)
    _add_traces(db_today, [X_CONTRA, Y_UNMEAS])
    ret_today = _run_writeback(db_today)
    snap_today = _apm(db_today)

    for knob in ("off", "0"):
        db = make_db()
        _add_traces(db, [X_CONTRA, Y_UNMEAS])
        _add_verifier(db, "vr-fail", "run-alpha", "fail")
        assert _run_writeback(db, knob=knob) == ret_today
        assert _apm(db) == snap_today


def test_input_false_positive_pass_also_drops_the_row(make_db):
    db = make_db()
    _add_traces(db, [X_CONTRA, Y_UNMEAS])
    _add_verifier(db, "vr-flagged", "run-alpha", "pass", is_false_positive=1)
    ret = _run_writeback(db)
    assert ret == 1
    assert ("agent-x", "code-fix") not in _apm(db)


def test_dropped_row_keeps_its_projected_reward(make_db, _no_cn):
    """The filter must not touch reward()'s arithmetic."""
    db = make_db()
    _add_traces(db, [_trace("t-x1", "run-alpha", "agent-x", cost=0.10), Y_UNMEAS])
    _add_verifier(db, "vr-fail", "run-alpha", "fail")
    _run_writeback(db)
    projected = [
        n for call in _no_cn for n in call["nodes"]
        if n.get("label") == "Trace" and n.get("id") == "t-x1"
    ]
    assert projected
    assert projected[0]["props"]["reward_g"] == pytest.approx(0.85)


def test_classification_memoized_once_per_distinct_run_id(make_db, monkeypatch):
    traces = [
        _trace("t-a1", "run-alpha", "agent-x"),
        _trace("t-a2", "run-alpha", "agent-x"),
        _trace("t-a3", "run-alpha", "agent-x"),
        _trace("t-b1", "run-beta", "agent-y"),
        _trace("t-b2", "run-beta", "agent-y"),
    ]
    real = reward_audit.classify_run
    calls = []

    def counting(con, run_id):
        calls.append(run_id)
        return real(con, run_id)

    monkeypatch.setattr(reward_audit, "classify_run", counting)
    db = make_db()
    _add_traces(db, traces)
    assert writeback.write_grpo_advantages(db) == 2
    assert sorted(calls) == ["run-alpha", "run-beta"]
    assert len(calls) == 2  # once per distinct run, not once per row (5 rows)


def test_enqueue_failure_never_changes_return_or_raises(make_db, monkeypatch):
    db_ok = make_db()
    _add_traces(db_ok, [X_CONTRA, Y_UNMEAS])
    _add_verifier(db_ok, "vr-fail", "run-alpha", "fail")
    ret_ok = writeback.write_grpo_advantages(db_ok)
    snap_ok = _apm(db_ok)

    def _boom(*args, **kwargs):
        raise RuntimeError("oversight inbox down")

    monkeypatch.setattr(oversight_inbox, "enqueue", _boom)
    db_err = make_db()
    _add_traces(db_err, [X_CONTRA, Y_UNMEAS])
    _add_verifier(db_err, "vr-fail", "run-alpha", "fail")
    ret_err = writeback.write_grpo_advantages(db_err)
    assert ret_err == ret_ok
    assert _apm(db_err) == snap_ok


# ── oversight inbox ──────────────────────────────────────────────────────────


def test_divergence_kind_is_queryable_by_column_equality(make_db):
    db = make_db()
    _add_traces(db, [X_CONTRA, Y_UNMEAS])
    _add_verifier(db, "vr-fail", "run-alpha", "fail")
    _run_writeback(db)
    rows = _gate_rows(db)
    assert len(rows) == 1
    assert "run-alpha" in " ".join(
        str(rows[0][k] or "") for k in ("feature", "phase", "context_json")
    )
    assert rows[0]["status"] == "pending"


def test_one_oversight_row_per_contradicted_run(make_db):
    db = make_db()
    _add_traces(db, [X_CONTRA, Y_UNMEAS, _trace("t-z1", "run-gamma", "agent-z", cost=0.30)])
    _add_verifier(db, "vr-f1", "run-alpha", "fail")
    _add_verifier(db, "vr-f2", "run-gamma", "fail")
    _run_writeback(db)
    rows = _gate_rows(db)
    assert len(rows) == 2
    named = " ".join(
        " ".join(str(r[k] or "") for k in ("feature", "phase", "context_json"))
        for r in rows
    )
    assert "run-alpha" in named and "run-gamma" in named


def test_shadow_keeps_rows_but_enqueues(make_db):
    db = make_db()
    _add_traces(db, [X_CONTRA, Y_UNMEAS])
    _add_verifier(db, "vr-fail", "run-alpha", "fail")
    ret = _run_writeback(db, knob="shadow")
    assert ret == 2
    assert set(_apm(db)) == {("agent-x", "code-fix"), ("agent-y", "code-fix")}
    rows = _gate_rows(db)
    assert len(rows) == 1
    assert "run-alpha" in str(rows[0]["feature"])


def test_enqueue_divergences_skips_malformed_and_caps(make_db, monkeypatch):
    db = make_db()
    monkeypatch.setenv("MO_REWARD_DIVERGENCE_ENQUEUE_LIMIT", "2")
    entries = [
        {"run_id": "run-1", "trace_id": "t-1", "reason": "r"},
        None,
        "garbage",
        7,
        {},
        {"run_id": ""},
        {"run_id": "run-2", "trace_id": "t-2", "reason": "r"},
        {"run_id": "run-3", "trace_id": "t-3", "reason": "r"},
    ]
    assert reward_audit.enqueue_divergences(db, entries) == 2
    assert len(_gate_rows(db)) == 2


# ── audit ────────────────────────────────────────────────────────────────────


def test_audit_counts_and_divergence_shape(make_db):
    db = make_db()
    _add_traces(db, [
        _trace("t-backed", "run-backed"),
        _trace("t-contra", "run-contra"),
        _trace("t-unmeas", "run-unmeas"),
        _trace("t-empty", ""),
        _trace("t-null", None),
    ])
    _add_verifier(db, "vr-pass", "run-backed", "pass")
    _add_verifier(db, "vr-fail", "run-contra", "fail")

    report = reward_audit.audit(db)
    assert report["checked"] == 3  # the two run-less traces never join
    assert report["backed"] == 1
    assert report["contradicted"] == 1
    assert report["unmeasured"] == 1
    assert len(report["divergences"]) == 1
    entry = report["divergences"][0]
    assert entry["run_id"] == "run-contra"
    assert entry["trace_id"] == "t-contra"
    assert entry["status"] == "success"
    assert isinstance(entry["reason"], str) and entry["reason"].strip()


def test_audit_missing_verifier_table_degrades(make_db):
    db = make_db(with_verifier_results=False)
    _add_traces(db, [_trace("t-a", "run-a"), _trace("t-b", "run-b")])
    report = reward_audit.audit(db)
    assert report["checked"] == 2
    assert report["unmeasured"] == 2
    assert report["divergences"] == []


def test_audit_limit_keeps_newest_rows(make_db):
    db = make_db()
    con = _connect(db)
    try:
        con.executemany(
            f"INSERT INTO execution_traces ({TRACE_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                _trace("t-new", "run-new"),
                _trace("t-old", "run-old"),
            ],
        )
        con.execute("UPDATE execution_traces SET created_at='2026-10-19T10:00:00Z' "
                    "WHERE trace_id='t-old'")
        con.commit()
    finally:
        con.close()
    report = reward_audit.audit(db, limit=1)
    assert report["checked"] == 1
    assert report["divergences"] == []
