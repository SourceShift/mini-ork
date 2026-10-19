"""Unit tests for the oracle-integrity audit and its promotion-gate consumer.

Hermetic: scripted judges/cases for the audit semantics, a tiny sqlite fixture for
the promotion guard, and one smoke that exercises the real ``oracle.judge`` with a
scripted runner/dispatch (no network, no model, no sandbox).
"""
from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import pytest

from mini_ork.gates import oracle_integrity as oi
from mini_ork.gates import promotion_gate

INJECTION_NAMES = [
    "silent_payload_non_delivery",
    "frozen_result_swap",
    "apply_fail_masked_as_pass",
    "stale_evidence_replay",
    "runner_error_as_pass",
    "test_defect_as_failure",
    "contract_violating_outcome",
]
CONTROL_NAMES = list(oi.CONTROLS)

AUDIT_RESULT_KEYS = {
    "pass", "detected", "missed", "controls_ok", "controls_failed",
    "judge", "audited_at",
}

#: The 10-key promotion result shape when both optional consumers are off.
LEGACY_RESULT_KEYS = {
    "decision", "rationale", "utility_before", "utility_after",
    "utility_delta", "benchmark_run_id", "n_runs", "all_pass",
    "safety_violations", "verifier_audit",
}

CANDIDATE = "cand-oi"


# ── helpers ──────────────────────────────────────────────────────────────────


def _forbid_judge(*_args, **_kwargs):
    raise AssertionError("audit must bind the judge through case.run")


def _decider(decision: str):
    def run() -> str:
        return decision
    return run


def _scripted_cases(decisions: dict[str, str]) -> list[oi.Case]:
    cases = [
        oi.Case(name=name, injected=True, run=_decider(decisions[name]))
        for name in INJECTION_NAMES
    ]
    cases += [
        oi.Case(name=name, injected=False, run=_decider(decisions[name]))
        for name in CONTROL_NAMES
    ]
    return cases


def _honest_decisions() -> dict[str, str]:
    decisions = {name: "REFUTED" for name in INJECTION_NAMES}
    decisions["silent_payload_non_delivery"] = "UNVERIFIED"
    decisions["test_defect_as_failure"] = "UNVERIFIED"
    decisions["known_good_proven"] = "PROVEN"
    decisions["known_bad_refuted"] = "REFUTED"
    decisions["known_empty_abstains"] = "UNVERIFIED"
    return decisions


# ── public surface ───────────────────────────────────────────────────────────


def test_injections_are_exactly_the_seven_named_builders():
    assert isinstance(oi.INJECTIONS, dict)
    assert list(oi.INJECTIONS) == INJECTION_NAMES
    assert all(callable(builder) for builder in oi.INJECTIONS.values())


def test_controls_are_exactly_the_three_known_answers():
    assert isinstance(oi.CONTROLS, tuple)
    assert oi.CONTROLS == (
        "known_good_proven", "known_bad_refuted", "known_empty_abstains",
    )


def test_case_is_a_frozen_dataclass():
    import dataclasses

    case = oi.Case(name="x", injected=True, run=_decider("PROVEN"))
    assert case.run() == "PROVEN"
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.name = "y"


# ── audit() semantics ────────────────────────────────────────────────────────


def test_honest_judge_detects_every_injection_and_reproduces_controls():
    result = oi.audit(_forbid_judge, cases=_scripted_cases(_honest_decisions()))
    assert set(result) == AUDIT_RESULT_KEYS
    assert result["pass"] is True
    assert sorted(result["detected"]) == sorted(INJECTION_NAMES)
    assert result["missed"] == []
    assert sorted(result["controls_ok"]) == sorted(CONTROL_NAMES)
    assert result["controls_failed"] == []
    assert isinstance(result["audited_at"], int)
    assert not isinstance(result["audited_at"], bool)


def test_holey_judge_returning_proven_on_apply_fail_is_missed():
    decisions = _honest_decisions()
    decisions["apply_fail_masked_as_pass"] = "PROVEN"
    result = oi.audit(_forbid_judge, cases=_scripted_cases(decisions))
    assert "apply_fail_masked_as_pass" in result["missed"]
    assert "apply_fail_masked_as_pass" not in result["detected"]
    assert result["pass"] is False
    assert result["pass"] == (
        result["missed"] == [] and result["controls_failed"] == []
    )


def test_always_abstaining_judge_fails_the_anti_theater_control():
    decisions = {name: "UNVERIFIED" for name in INJECTION_NAMES}
    decisions.update({name: "UNVERIFIED" for name in CONTROL_NAMES})
    result = oi.audit(_forbid_judge, cases=_scripted_cases(decisions))
    assert sorted(result["detected"]) == sorted(INJECTION_NAMES)
    assert result["missed"] == []
    assert "known_good_proven" in result["controls_failed"]
    assert result["pass"] is False


def test_wrong_control_answer_is_a_control_failure():
    decisions = _honest_decisions()
    decisions["known_bad_refuted"] = "UNVERIFIED"
    result = oi.audit(_forbid_judge, cases=_scripted_cases(decisions))
    assert "known_bad_refuted" in result["controls_failed"]
    assert "known_good_proven" in result["controls_ok"]
    assert result["pass"] is False


def test_crashing_case_is_scored_as_unverified():
    def boom() -> str:
        raise RuntimeError("judge crashed on bad evidence")

    cases = [
        oi.Case(name="known_good_proven", injected=False, run=boom),
        oi.Case(name="injection", injected=True, run=boom),
    ]
    result = oi.audit(_forbid_judge, cases=cases)
    assert result["detected"] == ["injection"]
    assert result["controls_failed"] == ["known_good_proven"]


# ── persistence ──────────────────────────────────────────────────────────────


def test_write_latest_and_latest_round_trip(tmp_path):
    db_path = str(tmp_path / "oi.db")
    record = {
        "pass": True,
        "detected": list(INJECTION_NAMES),
        "missed": [],
        "controls_ok": list(CONTROL_NAMES),
        "controls_failed": [],
        "judge": "mini_ork.certify.oracle.judge",
        "audited_at": 1760000000,
    }
    path = oi.write_latest(db_path, record)
    assert path.endswith("oracle-integrity/latest.json")
    assert Path(path).is_file()
    assert oi.latest(db_path) == record


def test_latest_is_none_when_absent_or_corrupt(tmp_path):
    assert oi.latest(str(tmp_path / "missing.db")) is None

    db_path = str(tmp_path / "oi.db")
    path = oi.write_latest(db_path, {"pass": True, "audited_at": 1})
    Path(path).write_text("{not json", encoding="utf-8")
    assert oi.latest(db_path) is None


# ── promotion-gate consumer ──────────────────────────────────────────────────


def _seed_db(tmp_path: Path) -> str:
    """Minimal DB that drives promotion_evaluate to the promote branch."""
    db = tmp_path / "promotion.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE benchmark_results (
            candidate_id TEXT,
            pass         INTEGER,
            utility_score REAL,
            run_id       TEXT,
            benchmark_id TEXT
        );
        CREATE TABLE benchmark_tasks (
            benchmark_id TEXT PRIMARY KEY,
            task_class   TEXT
        );
        CREATE TABLE workflow_candidates (
            candidate_id             TEXT PRIMARY KEY,
            base_workflow_version_id TEXT NOT NULL
        );
        CREATE TABLE promotion_records (
            promotion_id     TEXT PRIMARY KEY,
            candidate_id     TEXT,
            from_version_id  TEXT,
            to_version_id    TEXT,
            utility_before   REAL,
            utility_after    REAL,
            benchmark_run_id TEXT,
            rationale        TEXT,
            decision         TEXT,
            decided_by       TEXT
        );
    """)
    con.execute("INSERT INTO benchmark_tasks VALUES ('b1', 'framework_edit')")
    for run_id in ("r1", "r2", "r3"):
        con.execute(
            "INSERT INTO benchmark_results VALUES (?, 1, 0.5, ?, 'b1')",
            (CANDIDATE, run_id),
        )
    con.execute(
        "INSERT INTO workflow_candidates VALUES (?, 'v-base')", (CANDIDATE,)
    )
    con.commit()
    con.close()
    return str(db)


def _quiet_gate_env(monkeypatch, *, oracle_knob=None) -> None:
    if oracle_knob is None:
        monkeypatch.delenv("MO_ORACLE_INTEGRITY", raising=False)  # default "1"
    else:
        monkeypatch.setenv("MO_ORACLE_INTEGRITY", oracle_knob)
    monkeypatch.setenv("MO_PROMOTION_VERIFIER_AUDIT", "0")
    monkeypatch.setenv("MO_PROMOTION_GATE_HACKABILITY", "0")
    monkeypatch.delenv("MO_PROMOTION_MIN_RUNS", raising=False)


def _failing_record() -> dict:
    return {
        "pass": False,
        "detected": [name for name in INJECTION_NAMES
                     if name != "apply_fail_masked_as_pass"],
        "missed": ["apply_fail_masked_as_pass"],
        "controls_ok": ["known_bad_refuted", "known_empty_abstains"],
        "controls_failed": ["known_good_proven"],
        "judge": "",
        "audited_at": 1760000000,
    }


def _passing_record() -> dict:
    return {
        "pass": True,
        "detected": list(INJECTION_NAMES),
        "missed": [],
        "controls_ok": list(CONTROL_NAMES),
        "controls_failed": [],
        "judge": "",
        "audited_at": 1760000000,
    }


def _flatten(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _flatten(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _flatten(value)


def test_failed_audit_quarantines_instead_of_promoting(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    _quiet_gate_env(monkeypatch)
    oi.write_latest(db, _failing_record())
    result = promotion_gate.promotion_evaluate(db, CANDIDATE)
    assert result["decision"] == "quarantined"
    assert result["oracle_integrity"]["reason"] == "oracle_integrity_failed"
    strings = list(_flatten(result))
    assert any("apply_fail_masked_as_pass" in s for s in strings)
    assert any("known_good_proven" in s for s in strings)


def test_passed_audit_promotes_and_reports_pass(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    _quiet_gate_env(monkeypatch)
    oi.write_latest(db, _passing_record())
    result = promotion_gate.promotion_evaluate(db, CANDIDATE)
    assert result["decision"] == "promoted"
    assert result["oracle_integrity"]["status"] == "pass"
    assert result["oracle_integrity"]["audited_at"] == 1760000000


def test_missing_record_is_unmeasured_and_never_blocks(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    _quiet_gate_env(monkeypatch)
    result = promotion_gate.promotion_evaluate(db, CANDIDATE)
    assert result["decision"] == "promoted"
    assert result["oracle_integrity"] == {"status": "unmeasured"}


def test_knob_off_ignores_record_and_keeps_legacy_keys(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    _quiet_gate_env(monkeypatch, oracle_knob="0")
    oi.write_latest(db, _failing_record())
    result = promotion_gate.promotion_evaluate(db, CANDIDATE)
    assert result["decision"] == "promoted"
    assert "oracle_integrity" not in result
    assert set(result) == LEGACY_RESULT_KEYS


def test_hackable_gate_reject_still_wins(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    monkeypatch.delenv("MO_ORACLE_INTEGRITY", raising=False)
    monkeypatch.setenv("MO_PROMOTION_VERIFIER_AUDIT", "0")
    monkeypatch.delenv("MO_PROMOTION_GATE_HACKABILITY", raising=False)  # ON
    monkeypatch.delenv("MO_PROMOTION_MIN_RUNS", raising=False)
    oi.write_latest(db, _failing_record())
    monkeypatch.setattr(
        "mini_ork.gates.hackability.promotion_check",
        lambda db_path, task_class: {
            "over_threshold": [{"gate_id": "spec-gate", "hackability": 0.9}],
            "max": 0.25,
        },
    )
    result = promotion_gate.promotion_evaluate(db, CANDIDATE)
    assert result["decision"] == "rejected"
    assert "gate-hackability" in result["rationale"]


# ── the real judge, hermetically ─────────────────────────────────────────────


def test_run_default_audit_covers_all_ten_names_with_the_real_judge(monkeypatch):
    def _no_network(*_args, **_kwargs):
        raise AssertionError("run_default_audit attempted network access")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)

    result = oi.run_default_audit()
    assert set(result) == AUDIT_RESULT_KEYS
    covered = (
        list(result["detected"]) + list(result["missed"])
        + list(result["controls_ok"]) + list(result["controls_failed"])
    )
    assert len(covered) == 10
    assert sorted(covered) == sorted(INJECTION_NAMES + CONTROL_NAMES)
    assert result["pass"] == (
        result["missed"] == [] and result["controls_failed"] == []
    )
    # The controls are the anti-theater proof: the real judge must reproduce all
    # three known answers, otherwise the audit says nothing.
    assert sorted(result["controls_ok"]) == sorted(CONTROL_NAMES)
