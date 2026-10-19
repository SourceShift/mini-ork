"""Unit tests for ``mini_ork.gates.judge_validity`` and its promotion guard.

Kickoff G10-T03: measure each judge lane's approvals against
execution-grounded ``verifier_results`` evidence, emit a per-lane agreement
envelope, and quarantine a promotion when a measured lane falls below the
agreement floor.

The fixtures build real temp SQLite history DBs with the production column
names (``execution_traces(run_id, agent_version_id, reviewer_verdict)`` and
``verifier_results(run_id, verdict, is_false_positive)``) so a schema drift
fails here rather than silently degrading to "no history" in production.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from mini_ork.gates import judge_validity
from mini_ork.gates.promotion_gate import promotion_evaluate

HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS execution_traces (
    trace_id            TEXT PRIMARY KEY,
    run_id              TEXT,
    task_class          TEXT,
    reviewer_verdict    TEXT,
    verifier_output     TEXT,
    status              TEXT,
    agent_version_id    TEXT,
    workflow_version_id TEXT,
    created_at          INTEGER
);
CREATE TABLE IF NOT EXISTS verifier_results (
    result_id          TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    verifier_name      TEXT,
    verdict            TEXT NOT NULL,
    is_false_positive  INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER
);
"""

GATE_DDL = """
CREATE TABLE IF NOT EXISTS promotion_records (
    promotion_id        TEXT PRIMARY KEY,
    candidate_id        TEXT NOT NULL,
    from_version_id     TEXT NOT NULL,
    to_version_id       TEXT NOT NULL,
    decision            TEXT NOT NULL CHECK(decision IN (
                            'promoted','quarantined','rejected',
                            'pending_human_approval')),
    rationale           TEXT,
    utility_before      REAL,
    utility_after       REAL,
    benchmark_run_id    TEXT,
    decided_by          TEXT NOT NULL DEFAULT 'gate',
    decided_at          TEXT
);
CREATE TABLE IF NOT EXISTS workflow_candidates (
    candidate_id             TEXT PRIMARY KEY,
    base_workflow_version_id TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_results (
    benchmark_id   TEXT,
    candidate_id   TEXT,
    run_id         TEXT,
    pass           INTEGER,
    utility_score  REAL
);
"""

LEGACY_RESULT_KEYS = {
    "decision", "rationale", "utility_before", "utility_after",
    "utility_delta", "benchmark_run_id", "n_runs", "all_pass",
    "safety_violations", "verifier_audit",
}


# ── history fixtures ────────────────────────────────────────────────────────


def _history_db(tmp_path, name="history.db"):
    path = tmp_path / name
    con = sqlite3.connect(str(path))
    con.executescript(HISTORY_DDL)
    con.commit()
    con.close()
    return str(path)


def _populate(db_path, rows):
    """rows: ``(agent_version_id, reviewer_verdict, verifier_rows)``.

    ``verifier_rows`` is a list of ``(verdict, is_false_positive)`` pairs; an
    empty list is the kickoff's "no rows -> skip the run (unmeasured)" case.
    """
    con = sqlite3.connect(db_path)
    con.executescript(HISTORY_DDL)
    for idx, (av, verdict, verifier_rows) in enumerate(rows):
        run_id = f"run-{idx:05d}"
        con.execute(
            "INSERT INTO execution_traces (trace_id, run_id, reviewer_verdict,"
            " agent_version_id, created_at) VALUES (?,?,?,?,?)",
            (f"trace-{idx:05d}", run_id, verdict, av, idx),
        )
        for j, (v, fp) in enumerate(verifier_rows):
            con.execute(
                "INSERT INTO verifier_results (result_id, run_id, verdict,"
                " is_false_positive, created_at) VALUES (?,?,?,?,?)",
                (f"vr-{idx:05d}-{j}", run_id, v, fp, idx),
            )
    con.commit()
    con.close()


def _good_db(tmp_path):
    db = _history_db(tmp_path, name="good.db")
    _populate(db, [("claude-opus-4", "approve", [("pass", 0)])] * 30)
    return db


def _bad_db(tmp_path):
    db = _history_db(tmp_path, name="bad.db")
    rows = [("glm-4.7", "reject", [("fail", 0)])] * 18
    rows += [("glm-4.7", "approve", [("fail", 0)])] * 12
    _populate(db, rows)
    return db


# ── (1) wilson_lower ────────────────────────────────────────────────────────


def test_wilson_lower_n_zero_is_zero():
    assert judge_validity.wilson_lower(0, 0) == 0.0


def test_wilson_lower_no_successes_is_zero():
    assert judge_validity.wilson_lower(0, 10) == 0.0


def test_wilson_lower_all_successes_below_one_above_floor():
    lower = judge_validity.wilson_lower(30, 30)
    assert 0.8 < lower < 1.0


def test_wilson_lower_monotone_in_successes():
    values = [judge_validity.wilson_lower(k, 30) for k in range(31)]
    assert values == sorted(values)
    assert values[10] < values[20]


def test_wilson_lower_monotone_in_n_at_fixed_proportion():
    # p-hat = 0.6 at n = 10, 30, 50: more evidence, tighter interval.
    assert (
        judge_validity.wilson_lower(6, 10)
        < judge_validity.wilson_lower(18, 30)
        < judge_validity.wilson_lower(30, 50)
    )


def test_wilson_lower_smaller_z_narrows_interval():
    assert judge_validity.wilson_lower(30, 30, z=1.0) > (
        judge_validity.wilson_lower(30, 30)
    )


# ── (2) agreement ───────────────────────────────────────────────────────────


def test_agreement_below_overlap_is_unmeasured_and_admits():
    out = judge_validity.agreement([(True, True)] * 5)
    assert out["measured"] is False
    assert out["n"] == 5
    assert out["accuracy"] is None
    assert out["false_approve"] is None
    assert out["false_reject"] is None
    assert out["accuracy_lower"] is None
    assert out["admit"] is True
    assert out["reason"] == "insufficient overlap (n=5)"


def test_agreement_zero_rows_admits():
    out = judge_validity.agreement([])
    assert out["measured"] is False
    assert out["admit"] is True
    assert out["reason"] == "insufficient overlap (n=0)"


def test_agreement_overlap_boundary_is_inclusive():
    assert judge_validity.agreement([(True, True)] * 29)["measured"] is False
    assert judge_validity.agreement([(True, True)] * 30)["measured"] is True


def test_agreement_all_agree_uses_wilson_lower():
    out = judge_validity.agreement([(True, True)] * 30)
    assert out["measured"] is True
    assert out["accuracy"] == pytest.approx(1.0)
    assert out["false_approve"] == pytest.approx(0.0)
    assert out["false_reject"] == pytest.approx(0.0)
    assert out["accuracy_lower"] == pytest.approx(
        judge_validity.wilson_lower(30, 30)
    )
    assert out["admit"] is True


def test_agreement_point_six_accuracy_rejects():
    rows = [(True, True)] * 18 + [(True, False)] * 12
    out = judge_validity.agreement(rows)
    assert out["measured"] is True
    assert out["accuracy"] == pytest.approx(0.6)
    assert out["false_approve"] == pytest.approx(0.4)
    assert out["false_reject"] == pytest.approx(0.0)
    assert out["accuracy_lower"] == pytest.approx(
        judge_validity.wilson_lower(18, 30)
    )
    assert out["admit"] is False
    assert any(ch.isdigit() for ch in out["reason"])


def test_agreement_min_overlap_kwarg():
    out = judge_validity.agreement([(True, True)] * 5, min_overlap=5)
    assert out["measured"] is True
    assert out["min_overlap"] == 5
    assert out["accuracy"] == pytest.approx(1.0)


def test_agreement_min_env_is_tighten_only(monkeypatch):
    rows = [(True, True)] * 21 + [(True, False)] * 9
    monkeypatch.delenv("MO_JUDGE_VALIDITY_MIN", raising=False)
    assert judge_validity.agreement(rows)["admit"] is False
    # A lower env value must be clamped UP to the 0.80 default, never honored.
    monkeypatch.setenv("MO_JUDGE_VALIDITY_MIN", "0.5")
    assert judge_validity.agreement(rows)["admit"] is False


def test_agreement_min_env_can_tighten(monkeypatch):
    rows = [(True, True)] * 30
    monkeypatch.delenv("MO_JUDGE_VALIDITY_MIN", raising=False)
    assert judge_validity.agreement(rows)["admit"] is True
    monkeypatch.setenv("MO_JUDGE_VALIDITY_MIN", "0.99")
    assert judge_validity.agreement(rows)["admit"] is False


# ── (3) audit ───────────────────────────────────────────────────────────────


def test_audit_judge_and_execution_agree(tmp_path):
    db = _history_db(tmp_path)
    _populate(db, [("claude-opus-4", "approve", [("pass", 0)])] * 30)
    out = judge_validity.audit(db)
    assert out["rows"] == 30
    assert out["admit"] is True
    assert out["lanes"]["opus"]["n"] == 30
    assert out["lanes"]["opus"]["accuracy"] == pytest.approx(1.0)
    assert isinstance(out["audited_at"], int)


def test_audit_execution_side_is_independent_of_judge_side(tmp_path):
    # The judge approved ALL 30 runs; only ``verifier_results`` says half of
    # them failed. If the execution side came from reviewer_verdict, accuracy
    # would be 1.0.
    db = _history_db(tmp_path)
    rows = []
    for i in range(30):
        verdicts = [("pass", 0)] if i < 15 else [("fail", 0)]
        rows.append(("glm-4.7", "approve", verdicts))
    _populate(db, rows)
    out = judge_validity.audit(db)
    lane = out["lanes"]["glm"]
    assert lane["n"] == 30
    assert lane["accuracy"] == pytest.approx(0.5)
    assert lane["false_approve"] == pytest.approx(0.5)
    assert lane["false_reject"] == pytest.approx(0.0)
    assert out["admit"] is False


def test_audit_false_reject_is_reject_plus_pass(tmp_path):
    db = _history_db(tmp_path)
    rows = [("kimi-k2", "reject", [("pass", 0)])] * 2
    rows += [("kimi-k2", "reject", [("fail", 0)])] * 28
    _populate(db, rows)
    lane = judge_validity.audit(db)["lanes"]["kimi"]
    assert lane["accuracy"] == pytest.approx(28 / 30)
    assert lane["false_reject"] == pytest.approx(2 / 30)
    assert lane["false_approve"] == pytest.approx(0.0)


def test_audit_pass_requires_genuine_unflagged_pass(tmp_path):
    db = _history_db(tmp_path)
    _populate(db, [
        ("deepseek-v3", "approve", [("pass", 0)]),
        ("deepseek-v3", "approve", [("pass", 1)]),  # flagged: not a pass
        ("deepseek-v3", "approve", [("fail", 0), ("pass", 0)]),  # any pass wins
    ])
    lane = judge_validity.audit(db)["lanes"]["deepseek"]
    assert lane["n"] == 3
    assert lane["accuracy"] == pytest.approx(2 / 3)
    assert lane["false_approve"] == pytest.approx(1 / 3)


def test_audit_skips_unknown_verdicts_and_unmeasured_runs(tmp_path):
    db = _history_db(tmp_path)
    rows = [("opus-4", "approve", [("pass", 0)])] * 30
    rows.append(("opus-4", None, [("pass", 0)]))
    rows.append(("opus-4", "", [("pass", 0)]))
    rows.append(("opus-4", "sideways", [("pass", 0)]))
    rows.append(("opus-4", "approve", []))  # no verifier evidence
    _populate(db, rows)
    out = judge_validity.audit(db)
    assert out["rows"] == 30
    assert out["lanes"]["opus"]["n"] == 30


def test_audit_lane_family_tokens(tmp_path):
    db = _history_db(tmp_path)
    _populate(db, [
        ("claude-opus-4-20250115", "approve", [("pass", 0)]),
        ("glm-4.7", "approve", [("pass", 0)]),
        ("minimax-m2", "approve", [("pass", 0)]),
        ("deepseek-v3", "approve", [("pass", 0)]),
        ("kimi-k2", "approve", [("pass", 0)]),
        ("codex-1", "approve", [("pass", 0)]),
        ("mystery-model-v1", "approve", [("pass", 0)]),
        ("Opus-4-mixed-case", "approve", [("pass", 0)]),
    ])
    lanes = judge_validity.audit(db)["lanes"]
    assert set(lanes) == {
        "opus", "glm", "minimax", "deepseek", "kimi", "codex", "_unknown",
    }
    assert lanes["opus"]["n"] == 2


def test_audit_weak_lane_is_not_masked_by_strong_lane(tmp_path):
    db = _history_db(tmp_path)
    rows = [("claude-opus-4", "approve", [("pass", 0)])] * 30
    rows += [("glm-4.7", "reject", [("fail", 0)])] * 18
    rows += [("glm-4.7", "approve", [("fail", 0)])] * 12
    _populate(db, rows)
    out = judge_validity.audit(db)
    assert out["lanes"]["opus"]["admit"] is True
    assert out["lanes"]["glm"]["admit"] is False
    assert out["admit"] is False
    assert "glm" in out["reason"]
    assert any(ch.isdigit() for ch in out["reason"])


def test_audit_unmeasured_lane_never_blocks(tmp_path):
    db = _history_db(tmp_path)
    rows = [("claude-opus-4", "approve", [("pass", 0)])] * 30
    rows += [("glm-4.7", "approve", [("fail", 0)])] * 2  # n=2 < 30
    _populate(db, rows)
    out = judge_validity.audit(db)
    assert out["lanes"]["glm"]["measured"] is False
    assert out["lanes"]["glm"]["admit"] is True
    assert out["admit"] is True


def test_audit_limit_kwarg(tmp_path):
    db = _history_db(tmp_path)
    _populate(db, [("claude-opus-4", "approve", [("pass", 0)])] * 40)
    out = judge_validity.audit(db, limit=10)
    assert out["rows"] == 10
    assert out["lanes"]["opus"]["n"] == 10


def test_audit_missing_tables_is_no_history(tmp_path):
    empty_db = str(tmp_path / "empty.db")
    con = sqlite3.connect(empty_db)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    assert judge_validity.audit(empty_db) == {
        "rows": 0, "lanes": {}, "admit": True, "reason": "no history",
    }

    only_traces = str(tmp_path / "only_traces.db")
    con = sqlite3.connect(only_traces)
    con.execute(
        "CREATE TABLE execution_traces (trace_id TEXT PRIMARY KEY,"
        " run_id TEXT, reviewer_verdict TEXT, agent_version_id TEXT,"
        " created_at INTEGER)"
    )
    con.commit()
    con.close()
    assert judge_validity.audit(only_traces) == {
        "rows": 0, "lanes": {}, "admit": True, "reason": "no history",
    }


# ── (3b) envelopes ──────────────────────────────────────────────────────────


def test_write_envelopes_per_lane_atomic_sidecars(tmp_path):
    db = _history_db(tmp_path)
    rows = [("claude-opus-4", "approve", [("pass", 0)])] * 30
    rows += [("glm-4.7", "reject", [("fail", 0)])] * 18
    rows += [("glm-4.7", "approve", [("fail", 0)])] * 12
    _populate(db, rows)
    result = judge_validity.audit(db)
    paths = judge_validity.write_envelopes(db, result)

    out_dir = tmp_path / "judge-validity"
    assert {str(p) for p in paths} == {
        str(out_dir / "opus.json"), str(out_dir / "glm.json"),
    }
    for name in ("opus", "glm"):
        data = json.loads((out_dir / f"{name}.json").read_text())
        assert data["lane"] == name
        assert isinstance(data["audited_at"], int)
        assert {"n", "accuracy", "accuracy_lower"} & set(data)
    assert json.loads((out_dir / "glm.json").read_text())["n"] == 30


def test_write_envelopes_skips_unmeasured_and_no_history(tmp_path):
    db = _history_db(tmp_path)
    _populate(db, [("claude-opus-4", "approve", [("pass", 0)])] * 5)
    result = judge_validity.audit(db)
    assert result["admit"] is True
    assert judge_validity.write_envelopes(db, result) == []
    out_dir = tmp_path / "judge-validity"
    assert not out_dir.exists() or not any(out_dir.glob("*.json"))

    empty_db = str(tmp_path / "empty.db")
    sqlite3.connect(empty_db).close()
    no_history = judge_validity.audit(empty_db)
    assert judge_validity.write_envelopes(empty_db, no_history) == []


# ── (5) promotion_check ─────────────────────────────────────────────────────


def test_promotion_check_off_by_default_and_explicit_off(tmp_path, monkeypatch):
    db = _bad_db(tmp_path)
    monkeypatch.delenv("MO_JUDGE_VALIDITY", raising=False)
    assert judge_validity.promotion_check(db) is None
    for off in ("0", "off"):
        monkeypatch.setenv("MO_JUDGE_VALIDITY", off)
        assert judge_validity.promotion_check(db) is None


def test_promotion_check_unmeasured_returns_none(tmp_path, monkeypatch):
    empty_db = str(tmp_path / "empty.db")
    sqlite3.connect(empty_db).close()
    monkeypatch.setenv("MO_JUDGE_VALIDITY", "1")
    assert judge_validity.promotion_check(empty_db) is None


def test_promotion_check_runs_the_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_JUDGE_VALIDITY", "1")
    good = judge_validity.promotion_check(_good_db(tmp_path))
    assert isinstance(good, dict) and good["admit"] is True
    assert "opus" in good["lanes"]

    bad = judge_validity.promotion_check(_bad_db(tmp_path))
    assert isinstance(bad, dict) and bad["admit"] is False


def test_promotion_check_rows_limit_env(tmp_path, monkeypatch):
    db = _history_db(tmp_path, name="rows.db")
    _populate(db, [("claude-opus-4", "approve", [("pass", 0)])] * 40)
    monkeypatch.setenv("MO_JUDGE_VALIDITY", "1")
    monkeypatch.setenv("MO_JUDGE_VALIDITY_ROWS", "35")
    out = judge_validity.promotion_check(db)
    assert out["rows"] == 35
    assert out["lanes"]["opus"]["n"] == 35


def test_promotion_check_min_clamp_tighten_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_JUDGE_VALIDITY", "1")
    db = _history_db(tmp_path, name="mid.db")
    rows = [("claude-opus-4", "approve", [("pass", 0)])] * 21
    rows += [("claude-opus-4", "approve", [("fail", 0)])] * 9
    _populate(db, rows)
    monkeypatch.setenv("MO_JUDGE_VALIDITY_MIN", "0.5")  # clamps up to 0.80
    assert judge_validity.promotion_check(db)["admit"] is False

    monkeypatch.setenv("MO_JUDGE_VALIDITY_MIN", "0.99")  # genuine tightening
    assert judge_validity.promotion_check(_good_db(tmp_path))["admit"] is False


# ── (6) promotion-gate guard ────────────────────────────────────────────────


@pytest.fixture
def isolated_gate_env(monkeypatch):
    """Sibling guards off; judge-validity knobs at their defaults."""
    monkeypatch.setenv("MO_PROMOTION_VERIFIER_AUDIT", "0")
    monkeypatch.setenv("MO_PROMOTION_GATE_HACKABILITY", "0")
    monkeypatch.setenv("MO_ORACLE_INTEGRITY", "0")
    monkeypatch.delenv("MO_PROMOTION_MIN_RUNS", raising=False)
    monkeypatch.delenv("MO_JUDGE_VALIDITY", raising=False)
    monkeypatch.delenv("MO_JUDGE_VALIDITY_MIN", raising=False)
    monkeypatch.delenv("MO_JUDGE_VALIDITY_ROWS", raising=False)


def _gate_db(tmp_path, *, judge_history):
    """A DB whose candidate reaches the promote branch.

    ``judge_history``: None -> no history tables (unmeasured); "failing" ->
    opus 30/30 + glm at 0.6 accuracy; "passing" -> opus 30/30 only.
    """
    path = tmp_path / "gate.db"
    con = sqlite3.connect(str(path))
    con.executescript(GATE_DDL)
    idx = 0

    def add_run(agent_version_id, verdict, verifier_rows):
        nonlocal idx
        idx += 1
        run_id = f"run-{idx:05d}"
        con.execute(
            "INSERT INTO execution_traces (trace_id, run_id, reviewer_verdict,"
            " agent_version_id, created_at) VALUES (?,?,?,?,?)",
            (f"trace-{idx:05d}", run_id, verdict, agent_version_id, idx),
        )
        for j, (v, fp) in enumerate(verifier_rows):
            con.execute(
                "INSERT INTO verifier_results (result_id, run_id, verdict,"
                " is_false_positive, created_at) VALUES (?,?,?,?,?)",
                (f"vr-{idx:05d}-{j}", run_id, v, fp, idx),
            )

    if judge_history is not None:
        con.executescript(HISTORY_DDL)
        for _ in range(30):
            add_run("claude-opus-4", "approve", [("pass", 0)])
        if judge_history == "failing":
            for _ in range(18):
                add_run("glm-4.7", "reject", [("fail", 0)])
            for _ in range(12):
                add_run("glm-4.7", "approve", [("fail", 0)])

    con.execute(
        "INSERT INTO workflow_candidates (candidate_id,"
        " base_workflow_version_id) VALUES ('cand-1', 'ver-base')"
    )
    for k in range(3):
        for t in range(2):
            con.execute(
                "INSERT INTO benchmark_results (benchmark_id, candidate_id,"
                " run_id, pass, utility_score) VALUES (?,?,?,?,?)",
                (f"bench-{t}", "cand-1", f"benchrun-{k}", 1, 1.0),
            )
    con.commit()
    con.close()
    return str(path)


def test_gate_knob_off_leaves_legacy_shape(tmp_path, isolated_gate_env,
                                           monkeypatch):
    db = _gate_db(tmp_path, judge_history="failing")
    result = promotion_evaluate(db, "cand-1")
    assert result["decision"] == "promoted"
    assert "judge_validity" not in result
    assert set(result) == LEGACY_RESULT_KEYS

    for off in ("0", "off"):
        monkeypatch.setenv("MO_JUDGE_VALIDITY", off)
        result = promotion_evaluate(db, "cand-1")
        assert result["decision"] == "promoted"
        assert "judge_validity" not in result
        assert set(result) == LEGACY_RESULT_KEYS
    assert not (tmp_path / "judge-validity").exists()


def test_gate_failing_measured_lane_quarantines(tmp_path, isolated_gate_env,
                                                monkeypatch):
    db = _gate_db(tmp_path, judge_history="failing")
    monkeypatch.setenv("MO_JUDGE_VALIDITY", "1")
    result = promotion_evaluate(db, "cand-1")

    assert result["decision"] == "quarantined"
    assert "judge_validity_below_threshold" in json.dumps(result)
    assert result["judge_validity"]["admit"] is False
    # Per-lane envelopes, not one global blob: the weak lane cannot hide.
    sidecar = tmp_path / "judge-validity"
    assert (sidecar / "opus.json").is_file()
    assert (sidecar / "glm.json").is_file()


def test_gate_passing_lane_keeps_normal_decision(tmp_path, isolated_gate_env,
                                                 monkeypatch):
    db = _gate_db(tmp_path, judge_history="passing")
    monkeypatch.setenv("MO_JUDGE_VALIDITY", "1")
    result = promotion_evaluate(db, "cand-1")

    assert result["decision"] == "promoted"
    assert result["judge_validity"]["status"] == "measured"
    assert "reason" in result["judge_validity"]
    assert (tmp_path / "judge-validity" / "opus.json").is_file()


def test_gate_unmeasured_reports_status_and_promotes(tmp_path,
                                                     isolated_gate_env,
                                                     monkeypatch):
    db = _gate_db(tmp_path, judge_history=None)
    monkeypatch.setenv("MO_JUDGE_VALIDITY", "1")
    result = promotion_evaluate(db, "cand-1")

    assert result["decision"] == "promoted"
    assert result["judge_validity"]["status"] == "unmeasured"
    assert "reason" in result["judge_validity"]
    sidecar = tmp_path / "judge-validity"
    assert not sidecar.exists() or not any(sidecar.glob("*.json"))
