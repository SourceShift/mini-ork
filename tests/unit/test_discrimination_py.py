"""Unit tests for the discrimination term (kickoff: vt13-discrimination).

Hermetic: stub runners and a canned dispatch only — no network, no model, no
run-local state. Covers the `score` contract (tests 1-6) and its judge()
integration (test 7): PROVEN withheld to UNVERIFIED, never REFUTED.
"""
from __future__ import annotations

import inspect
import types

import pytest

from mini_ork.certify.discrimination import (
    DEFAULT_MIN_DISCRIMINATION,
    PROVEN,
    REFUTED,
    UNVERIFIED,
    Candidate,
    DiscriminationResult,
    score,
)
from mini_ork.certify.oracle import judge

TEST_SRC = "def test_poc_marker():\n    assert widget() == 42\n"
GOOD = ("POOL-GOOD-1", "POOL-GOOD-2")
BAD = ("POOL-BAD-1", "POOL-BAD-2")

ISSUE = "The reported bug: widget foo returns wrong values."
PATCH = "PATCH-FIX"
POC = "def test_poc():\n    assert widget() == 42\n"
MR_BLOCK = (
    "```python\n"
    "import math\n"
    "\n"
    "def test_mr_1():\n    assert math.sqrt(4) == 2\n"
    "\n"
    "def test_mr_2():\n    assert math.sqrt(16) == 4\n"
    "```\n"
)


# ── doubles ──────────────────────────────────────────────────────────────────


class ScriptedRunner:
    """Maps each patch string to a status; records every call."""

    up = True

    def __init__(self, patch_status=None, default_patch_status="failed", base_status="failed"):
        self.patch_status = dict(patch_status or {})
        self.default_patch_status = default_patch_status
        self.base_status = base_status
        self.calls = []

    def run_test(self, src=None, patch=None):
        self.calls.append((src, patch))
        status = (
            self.patch_status.get(patch, self.default_patch_status)
            if patch else self.base_status
        )
        return types.SimpleNamespace(status=status, exc=f"{status}-exc",
                                     output=f"{status}-out")

    def candidate_patches_run(self):
        return [p for (_, p) in self.calls if p and p.startswith(("POOL-", "CAND-"))]


def full_pool():
    return ([Candidate(patch=p, correct=True) for p in GOOD]
            + [Candidate(patch=p, correct=False) for p in BAD])


def separating_runner():
    return ScriptedRunner(patch_status={g: "passed" for g in GOOD})


def accepting_runner():
    return ScriptedRunner(default_patch_status="passed")


def rejecting_runner():
    return ScriptedRunner(default_patch_status="failed")


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    monkeypatch.delenv("MO_DISCRIMINATION_MIN", raising=False)
    monkeypatch.delenv("MO_ASSAY_DISCRIMINATION", raising=False)
    monkeypatch.setenv("MO_ASSAY_RELATIONS", "0")
    monkeypatch.setenv("MO_ASSAY_DIFFERENTIAL", "0")


# ── tests 1-3: the score itself ─────────────────────────────────────────────


def test_separating_pool_scores_one_and_is_proven():
    result = score(TEST_SRC, full_pool(), runner=separating_runner())
    assert isinstance(result, DiscriminationResult)
    assert result.status == PROVEN
    assert result.discrimination == 1.0
    assert (result.correct_accepted, result.correct_total) == (2, 2)
    assert (result.incorrect_rejected, result.incorrect_total) == (2, 2)


def test_accept_everything_is_refuted_and_names_accepted_incorrect():
    result = score(TEST_SRC, full_pool(), runner=accepting_runner())
    assert result.discrimination == 0.5
    assert result.status == REFUTED
    for patch in BAD:
        assert patch in result.evidence


def test_reject_everything_is_refuted_and_names_rejected_correct():
    result = score(TEST_SRC, full_pool(), runner=rejecting_runner())
    assert result.discrimination == 0.5
    assert result.status == REFUTED
    for patch in GOOD:
        assert patch in result.evidence


def test_runner_receives_the_test_source_and_each_candidate_patch():
    runner = separating_runner()
    score(TEST_SRC, full_pool(), runner=runner)
    assert len(runner.calls) == 4
    assert set(runner.candidate_patches_run()) == set(GOOD) | set(BAD)
    assert all(src == TEST_SRC for src, _ in runner.calls)


# ── test 4: one class empty → unmeasurable ──────────────────────────────────


def test_one_class_empty_is_unverified_with_no_score():
    only_correct = score(TEST_SRC, [Candidate(patch=p, correct=True) for p in GOOD],
                         runner=accepting_runner())
    assert only_correct.status == UNVERIFIED
    assert only_correct.discrimination is None
    assert only_correct.evidence.startswith("discrimination unmeasurable")

    only_incorrect = score(TEST_SRC, [Candidate(patch=p, correct=False) for p in BAD],
                           runner=rejecting_runner())
    assert only_incorrect.status == UNVERIFIED
    assert only_incorrect.discrimination is None

    empty = score(TEST_SRC, [], runner=accepting_runner())
    assert empty.status == UNVERIFIED
    assert empty.discrimination is None


# ── test 5: unmeasured candidates shrink the denominator ────────────────────


def test_error_candidate_is_excluded_from_both_sides():
    pool = [Candidate(patch=GOOD[0], correct=True), Candidate(patch=BAD[0], correct=False)]
    runner = ScriptedRunner(patch_status={GOOD[0]: "passed", BAD[0]: "error"})
    result = score(TEST_SRC, pool, runner=runner)
    assert result.discrimination == 1.0
    assert result.status == PROVEN


def test_every_unmeasured_status_is_ok_none_never_a_rejection():
    pool = [
        Candidate(patch=GOOD[0], correct=True),       # passed     -> accept
        Candidate(patch=BAD[0], correct=False),       # failed     -> reject
        Candidate(patch=GOOD[1], correct=True),       # error      -> unmeasured
        Candidate(patch=BAD[1], correct=False),       # test_defect-> unmeasured
        Candidate(patch="POOL-GOOD-3", correct=True),  # apply_fail -> unmeasured
        Candidate(patch="POOL-BAD-3", correct=False),  # no_run     -> unmeasured
    ]
    runner = ScriptedRunner(patch_status={
        GOOD[0]: "passed", BAD[0]: "failed",
        GOOD[1]: "error", BAD[1]: "test_defect",
        "POOL-GOOD-3": "apply_fail", "POOL-BAD-3": "no_run",
    })
    result = score(TEST_SRC, pool, runner=runner)
    # A harness status counted as a rejection would drag this to 2/3.
    assert result.discrimination == 1.0
    assert result.status == PROVEN

    unmeasured = [c for c in result.checks if c["ok"] is None]
    assert len(unmeasured) == 4
    for status in ("error", "test_defect", "apply_fail", "no_run"):
        assert any(status in str(c) for c in unmeasured)
    assert all({"name", "ok", "detail"} <= set(c) for c in unmeasured)
    measured = [c for c in result.checks if c["ok"] is not None]
    assert len(measured) == 2


# ── test 6: the tighten-only knob ───────────────────────────────────────────


def test_mo_discrimination_min_half_makes_half_proven_default_refuted(monkeypatch):
    monkeypatch.setenv("MO_DISCRIMINATION_MIN", "0.5")
    assert score(TEST_SRC, full_pool(), runner=accepting_runner()).status == PROVEN
    monkeypatch.delenv("MO_DISCRIMINATION_MIN", raising=False)
    assert score(TEST_SRC, full_pool(), runner=accepting_runner()).status == REFUTED


def test_out_of_range_min_falls_back_to_default(monkeypatch):
    for raw in ("1.5", "-0.25", "2"):
        monkeypatch.setenv("MO_DISCRIMINATION_MIN", raw)
        assert score(TEST_SRC, full_pool(), runner=accepting_runner()).status == REFUTED
        assert score(TEST_SRC, full_pool(), runner=separating_runner()).status == PROVEN


def test_default_minimum_is_one():
    assert DEFAULT_MIN_DISCRIMINATION == 1.0


# ── test 7: judge() integration ─────────────────────────────────────────────


def fake_dispatch(prompt):
    return MR_BLOCK


def discriminating_pool():
    return ([Candidate(patch=p, correct=True) for p in GOOD]
            + [Candidate(patch=p, correct=False) for p in BAD])


def call_judge(runner, **extra):
    return judge(ISSUE, PATCH, runner=runner, poc=POC, dispatch=fake_dispatch, **extra)


def test_judge_gains_exactly_two_optional_keyword_only_parameters():
    # `candidates` (discrimination) and `run_id` (hidden checks) are the only
    # optional additions to the judge's public signature.
    params = inspect.signature(judge).parameters
    assert list(params) == ["issue", "patch", "runner", "mr_n", "gold",
                            "poc", "dispatch", "context", "candidates", "run_id"]
    for name in ("candidates", "run_id"):
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is None
    required = {n for n, p in params.items() if p.default is inspect.Parameter.empty}
    assert required == {"issue", "patch", "runner"}


def _green_runner(**kwargs):
    """Patched runs pass unless the pool-override says otherwise (judge doubles)."""
    kwargs.setdefault("default_patch_status", "passed")
    return ScriptedRunner(**kwargs)


def test_green_patch_with_discriminating_pool_stays_proven():
    runner = _green_runner(patch_status={b: "failed" for b in BAD})
    verdict = call_judge(runner, candidates=discriminating_pool())
    assert verdict.verdict == PROVEN
    assert set(runner.candidate_patches_run()) == set(GOOD) | set(BAD)
    assert "discriminat" in str(verdict.detail).lower()


def test_green_patch_with_accept_everything_pool_becomes_unverified():
    verdict = call_judge(_green_runner(), candidates=discriminating_pool())
    assert verdict.verdict == UNVERIFIED
    assert verdict.verdict != REFUTED
    assert "does not discriminate" in verdict.reason


def test_unmeasurable_pool_leaves_proven_and_notes_unmeasured():
    runner = _green_runner(patch_status={c: "error" for c in GOOD + BAD})
    verdict = call_judge(runner, candidates=discriminating_pool())
    assert verdict.verdict == PROVEN
    blob = str(verdict.detail).lower()
    assert "discriminat" in blob and "unmeasur" in blob


def test_candidates_none_matches_omitted():
    omitted = call_judge(_green_runner())
    assert omitted == call_judge(_green_runner(), candidates=None)
    assert omitted.verdict == PROVEN


def test_knob_zero_and_off_disable_the_term(monkeypatch):
    for raw in ("0", "off"):
        monkeypatch.setenv("MO_ASSAY_DISCRIMINATION", raw)
        runner = _green_runner()  # accept-everything pool would downgrade if it ran
        verdict = call_judge(runner, candidates=discriminating_pool())
        assert verdict == call_judge(_green_runner())
        assert verdict.verdict == PROVEN
        assert runner.candidate_patches_run() == []


def test_poc_rejected_patch_never_runs_candidates_and_stays_refuted():
    runner = ScriptedRunner(patch_status={PATCH: "failed"})
    verdict = call_judge(runner, candidates=discriminating_pool())
    assert verdict.verdict == REFUTED
    assert runner.candidate_patches_run() == []


def test_mapping_and_candidate_like_entries_accepted_malformed_skipped():
    pool = [
        None,
        42,
        "junk",
        {},
        {"patch": "x"},
        {"correct": True},
        {"patch": GOOD[0], "correct": True},
        types.SimpleNamespace(patch=BAD[0], correct=False),
    ]
    runner = _green_runner(patch_status={b: "failed" for b in BAD})
    verdict = call_judge(runner, candidates=pool)
    assert verdict.verdict == PROVEN
    seen = set(runner.candidate_patches_run())
    assert GOOD[0] in seen
    assert BAD[0] in seen
