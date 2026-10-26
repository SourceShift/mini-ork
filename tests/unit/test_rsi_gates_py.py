"""Unit tests: the eleven merge gates, and the two red-baseline traps.

The gates decide whether a self-authored patch reaches ``main``, so the tests
are written the way an attacker would read them:

* **Missing evidence fails.** Every optional field defaults to a failing value,
  so a gate that was never fed cannot pass by omission.
* **A red baseline is not a permanent veto.** ``garden`` and ``validate`` are
  already red on a healthy checkout; G7 must pass when nothing *new* appeared,
  and G8 must pass when the candidate's own gap disappeared — even though the
  probe as a whole is still red.
* **Coexistence is not a fix.** A patch that leaves the baseline exactly as it
  found it passes G7 (nothing new) and fails G8 (the gap is still there).
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from mini_ork.rsi.gates import (
    FAIL_CODES,
    GATE_ORDER,
    CriticVerdict,
    GateEvidence,
    GateResult,
    differential,
    evaluate,
)
from mini_ork.rsi.probes import Finding

DEFAULT_GAP = Finding(level="error", message="output collision: 'synthesis.md'",
                      path="recipes/a/artifact_contract.yaml")
GARDEN_SECOND = Finding(level="error", message="output collision: 'verdict.json'",
                        path="recipes/b/artifact_contract.yaml")


def _critic(alias: str, lane: str, *, approve: bool = True) -> CriticVerdict:
    return CriticVerdict(alias=alias, lane=lane, approve=approve, confidence=0.9)


GOOD_CRITICS = (_critic("c1", "glm-5.3"), _critic("c2", "claude-opus-5-5"))


def _evidence(**kw) -> GateEvidence:
    """A record where all eleven gates pass. Overrides break one at a time."""
    base = dict(
        harvest_ok=True,
        child_verdict={"pass": True, "files_changed": 1, "tests_pass": True},
        frozen_hits=(),
        changed_files=("mini_ork/learning/ledger.py",),
        scope_files=("mini_ork/learning/ledger.py",),
        max_files=5,
        lint_ok=True,
        pytest_ok=True,
        static_before=(DEFAULT_GAP, GARDEN_SECOND),
        static_after=(GARDEN_SECOND,),
        static_ran=True,
        gap_before=True,
        gap_after=False,
        metric_command="db:L10",
        probe_failures_before=(),
        probe_failures_after=(),
        probes_ran=True,
        critics=GOOD_CRITICS,
        # The implementer lane is codex_lens (family openai): neither critic may
        # share it. anthropic here would collide with critic c2 and fail G10.
        implementer_family="codex_lens",
        critic_panel={"ok": True, "problems": []},
        spend_24h=10.0,
        daily_cap=50.0,
        cycle_cost=9.0,
        per_cycle_cap=20.0,
    )
    base.update(kw)
    return GateEvidence(**base)


def _codes(ev: GateEvidence) -> tuple[str, ...]:
    return evaluate(ev).codes


# ── the happy path ──────────────────────────────────────────────────────────


def test_all_eleven_gates_pass_on_complete_evidence():
    report = evaluate(_evidence())
    assert report.passed, [r.to_dict() for r in report.failing]
    assert len(report.results) == 11
    assert [r.gate for r in report.results] == [g for g, _, _ in GATE_ORDER]
    assert report.to_dict()["failed"] == []


def test_every_gate_is_evaluated_even_when_the_first_fails():
    codes = _codes(_evidence(harvest_ok=False, lint_ok=False, pytest_ok=False))
    assert {"harvest_incomplete", "lint_failed", "tests_failed"} <= set(codes)


# ── G1 harvest ──────────────────────────────────────────────────────────────


def test_incomplete_harvest_blocks_merge():
    assert "harvest_incomplete" in _codes(_evidence(harvest_ok=False))
    assert "harvest_incomplete" in _codes(
        _evidence(required_probe_failures=("garden",)))


# ── G2 child verdict ────────────────────────────────────────────────────────


def test_missing_or_failed_verdict_blocks_merge():
    assert "child_verdict_failed" in _codes(_evidence(child_verdict=None))
    assert "child_verdict_failed" in _codes(_evidence(child_verdict={"pass": False}))
    assert "child_verdict_failed" in _codes(
        _evidence(child_verdict={"pass": True, "files_changed": 0}))


# ── G3 / G4 scope ───────────────────────────────────────────────────────────


def test_frozen_path_blocks_merge():
    assert "frozen_path" in _codes(
        _evidence(frozen_hits=("config/kernel-frozen.yaml",)))


def test_scope_uses_the_same_allowlist_select_used():
    # an allowlisted path outside the kickoff list is tolerated by G4 ...
    ok = _evidence(changed_files=("mini_ork/learning/ledger.py",
                                  "mini_ork/learning/pattern_induction.py"))
    assert "scope_violation" not in _codes(ok)
    # ... but a path outside both lists is not
    bad = _evidence(changed_files=("README.md",))
    assert "scope_violation" in _codes(bad)


def test_scope_bounds_the_file_count_and_refuses_an_empty_change():
    six = tuple(f"mini_ork/learning/f{i}.py" for i in range(6))
    assert "scope_violation" in _codes(_evidence(changed_files=six, max_files=5))
    assert "scope_violation" in _codes(_evidence(changed_files=()))


# ── G5 / G6 fail closed ─────────────────────────────────────────────────────


def test_a_gate_that_did_not_run_is_not_a_pass():
    ev = _evidence(lint_ok=None, pytest_ok=None)
    codes = _codes(ev)
    assert "lint_failed" in codes and "tests_failed" in codes
    report = evaluate(ev)
    assert "did not run" in {r.code: r.detail for r in report.failing}["lint_failed"]


def test_a_reported_failure_keeps_its_detail():
    report = evaluate(_evidence(lint_ok=False, lint_detail="F821 undefined name"))
    gate = {r.gate: r for r in report.results}["G5"]
    assert gate.code == "lint_failed"
    assert gate.detail == "F821 undefined name"


# ── G7 the red-baseline gate ────────────────────────────────────────────────


def test_g7_passes_from_a_red_baseline_when_the_change_adds_nothing():
    # The plan's trap: garden is already red on a healthy checkout.
    ev = _evidence(static_before=(DEFAULT_GAP, GARDEN_SECOND),
                   static_after=(DEFAULT_GAP, GARDEN_SECOND))
    report = evaluate(ev)
    assert report.passed, [r.to_dict() for r in report.failing]
    assert {r.gate: r for r in report.results}["G7"].detail == "0 finding(s) removed"


def test_g7_fails_on_one_new_finding_even_from_a_red_baseline():
    new = Finding(level="warning", message="workflow exceeds 16 KiB",
                  path="recipes/x/workflow.yaml")
    codes = _codes(_evidence(static_after=(GARDEN_SECOND, new)))
    assert "new_static_finding" in codes


def test_g7_counts_a_removed_finding_as_progress():
    report = evaluate(_evidence(static_before=(DEFAULT_GAP, GARDEN_SECOND),
                                static_after=(GARDEN_SECOND,)))
    gate = {r.gate: r for r in report.results}["G7"]
    assert gate.ok and "1 finding(s) removed" in gate.detail


def test_g7_fails_closed_when_the_differential_did_not_run():
    assert "new_static_finding" in _codes(_evidence(static_ran=False))


def test_differential_compares_signatures_not_counts():
    replacement = Finding(level="error", message="a different message",
                          path="recipes/a/artifact_contract.yaml")
    new, removed, unchanged = differential((DEFAULT_GAP,), (replacement,))
    assert len(new) == 1 and len(removed) == 1 and unchanged == ()


def test_differential_collapses_duplicate_signatures():
    new, removed, unchanged = differential((DEFAULT_GAP, DEFAULT_GAP),
                                           (DEFAULT_GAP,))
    assert new == () and removed == () and len(unchanged) == 1


# ── G8 the success metric ───────────────────────────────────────────────────


def test_g8_passes_only_when_the_gap_disappeared():
    assert "metric_unchanged" not in _codes(_evidence(gap_before=True, gap_after=False))


def test_g8_fails_when_the_gap_is_still_there():
    report = evaluate(_evidence(gap_before=True, gap_after=True))
    gate = {r.gate: r for r in report.results}["G8"]
    assert gate.code == "metric_unchanged"
    assert "still present" in gate.detail


def test_g8_fails_when_the_gap_was_already_absent():
    report = evaluate(_evidence(gap_before=False, gap_after=False))
    gate = {r.gate: r for r in report.results}["G8"]
    assert "already absent" in gate.detail


def test_g8_fails_closed_when_the_metric_was_not_measured():
    report = evaluate(_evidence(gap_before=None, gap_after=False))
    assert {r.gate: r for r in report.results}["G8"].code == "metric_unchanged"
    assert "not measured" in {r.gate: r for r in report.results}["G8"].detail


# ── G9 probe differential ───────────────────────────────────────────────────


def test_g9_tolerates_a_pre_existing_probe_failure_and_refuses_a_new_one():
    assert "new_probe_failure" not in _codes(
        _evidence(probe_failures_before=("metrics",), probe_failures_after=("metrics",)))
    assert "new_probe_failure" in _codes(
        _evidence(probe_failures_before=(), probe_failures_after=("metrics",)))
    assert "new_probe_failure" in _codes(_evidence(probes_ran=False))


# ── G10 critics ─────────────────────────────────────────────────────────────


def test_g10_requires_two_unanimous_critics():
    assert "critic_split" in _codes(_evidence(
        critics=(_critic("c1", "glm-5.3"), _critic("c2", "claude-opus-5-5", approve=False))))
    minimal = _evidence(critics=(_critic("c1", "glm-5.3"), _critic("c2", "claude-opus-5-5")))
    assert "critic_split" not in _codes(minimal)


def test_g10_names_blocking_findings_as_dissent():
    ev = _evidence(critics=(
        CriticVerdict(alias="c1", lane="glm-5.3", approve=True, blocking=("breaks X",)),
        _critic("c2", "claude-opus-5-5"),
    ))
    assert "critic_split" in _codes(ev)


def test_g10_tells_unanimous_rejection_from_a_split():
    """A panel that agrees the fix is wrong is not a panel that disagrees.

    Both fail the gate, but they mean opposite things to the loop: unanimous
    rejection is evidence about the *change* (the fix shape is wrong — a second
    child should be aimed elsewhere), a split is evidence about the *question*
    (the gap is underspecified). Cycle 13 merged the two under
    ``critic_split`` and the quarantine read as "the critics could not agree"
    when in fact both had said no for the same reason.
    """
    both_no = _evidence(critics=(
        _critic("c1", "glm-5.3", approve=False),
        _critic("c2", "claude-opus-5-5", approve=False),
    ))
    assert "critic_rejected" in _codes(both_no)
    assert "critic_split" not in _codes(both_no)

    one_no = _evidence(critics=(
        _critic("c1", "glm-5.3", approve=False), _critic("c2", "claude-opus-5-5")))
    assert "critic_split" in _codes(one_no)
    assert "critic_rejected" not in _codes(one_no)

    # An approve-with-blocking-finding counts as dissent, so a panel where both
    # approve but both block is also a rejection.
    both_blocking = _evidence(critics=(
        CriticVerdict(alias="c1", lane="glm-5.3", approve=True, blocking=("x",)),
        CriticVerdict(alias="c2", lane="claude-opus-5-5", approve=True, blocking=("y",)),
    ))
    assert "critic_rejected" in _codes(both_blocking)


def test_g10_requires_distinct_families_and_no_implementer_family():
    same = _evidence(critics=(_critic("c1", "glm-5.3"), _critic("c2", "glm-4.6")))
    assert "critic_not_exogenous" in _codes(same)
    impl = _evidence(implementer_family="deepseek-v4")
    assert "critic_not_exogenous" not in _codes(impl)
    impl_same = _evidence(implementer_family="zai")
    assert "critic_not_exogenous" in _codes(impl_same)


def test_g10_fails_on_a_missing_or_non_unique_panel():
    assert "critic_not_exogenous" in _codes(_evidence(critics=(_critic("c1", "glm-5.3"),)))
    assert "critic_not_exogenous" in _codes(_evidence(
        critic_panel={"ok": False, "problems": ["fallback:c1:served=glm"]}))


def test_critic_family_is_resolved_from_the_served_lane():
    assert CriticVerdict(alias="x", lane="claude-opus-5-5").family == "anthropic"
    assert CriticVerdict(alias="x", lane="glm-5.3").family == "zai"
    assert CriticVerdict.from_mapping(
        {"alias": "c", "lane": "codex-mini", "approve": True,
         "blocking_findings": "x", "confidence": 0.7}
    ).blocking == ("x",)


def test_a_verdict_round_trips_through_its_ledger_shape():
    """``why`` is carried for the ledger and ignored by the gate.

    G10 grades ``approve`` + ``family`` + ``blocking``; the reasoning is kept
    beside them so a quarantined split explains itself without a run directory.
    """
    raw = {"alias": "minimax", "lane": "minimax_lens", "approve": False,
           "blocking_findings": ["nothing emits the field"], "confidence": 0.76,
           "why": "every verifier envelope lacks one"}
    verdict = CriticVerdict.from_mapping(raw)
    assert verdict.why == "every verifier envelope lacks one"
    assert verdict.to_dict() == {
        "alias": "minimax", "lane": "minimax_lens", "family": "minimax",
        "approve": False, "confidence": 0.76,
        "blocking_findings": ["nothing emits the field"],
        "why": "every verifier envelope lacks one",
    }
    # A verdict built by a test fake carries no prose and must not invent any.
    assert CriticVerdict("glm", lane="glm_lens", approve=True).to_dict()["why"] == ""


# ── G11 budget ──────────────────────────────────────────────────────────────


def test_g11_fails_closed_without_caps():
    assert "budget_exceeded" in _codes(_evidence(daily_cap=0.0))
    assert "budget_exceeded" in _codes(_evidence(per_cycle_cap=0.0))


def test_g11_refuses_over_budget():
    assert "budget_exceeded" in _codes(_evidence(spend_24h=50.0))
    assert "budget_exceeded" in _codes(_evidence(cycle_cost=25.0))


# ── record shapes ───────────────────────────────────────────────────────────


def test_gate_result_rejects_inconsistent_records():
    with pytest.raises(ValueError):
        GateResult("G1", "harvest_complete", True, "harvest_incomplete")
    with pytest.raises(ValueError):
        GateResult("G1", "harvest_complete", False, "not_a_code")


def test_fail_codes_cover_every_gate_and_g10_s_extra_codes():
    for code in (row[2] for row in GATE_ORDER):
        assert code in FAIL_CODES
    # G10's ledger spellings are deliberately three-way: the table's declared
    # code is the disagreement case, and the two extra codes name the ways a
    # panel can fail without disagreeing with itself.
    assert "critic_not_exogenous" in FAIL_CODES
    assert "critic_rejected" in FAIL_CODES


def test_replace_keeps_the_fixture_honest():
    ev = replace(_evidence(), gap_after=True)
    assert "metric_unchanged" in _codes(ev)
