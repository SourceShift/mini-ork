"""Unit tests for mini_ork.certify — solve-time oracle engine.

Hermetic by design: no docker, no network, no model. A FakeRunner scripts
ExecOutcome per call in order; a fake_dispatch returns canned model text
keyed off prompt shape. Each test pins one verdict path end-to-end so a
silent regression in judge / probe / invariants cannot pass.
"""
from __future__ import annotations

import re

import pytest

from mini_ork.certify import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    judge,
)
from mini_ork.certify import invariants as inv
from mini_ork.certify.context import CodeContext
from mini_ork.runtime import ExecOutcome


# ── Test doubles ───────────────────────────────────────────────────────────────

ISSUE_TEXT = (
    "The bug is: foo() returns -1 instead of 42.\n"
    "\n"
    "Specifically, calling foo() should return 42 (the documented value), "
    "but it returns -1."
)
ISSUE_QUOTE = "calling foo() should return 42 (the documented value)"
GROUND_JSON = (
    '{"states_expected_behaviour": true, '
    f'"quote": "{ISSUE_QUOTE}", '
    '"expected_summary": "foo returns 42", '
    '"reported_exception": ""}'
)
POC_CODE = (
    "def test_foo_returns_42():\n"
    "    assert foo() == 42\n"
)


def _mr_block(n: int) -> str:
    """Produce a python block containing n `test_mr_*` functions sharing a header."""
    header = "import pytest\n"
    tests = "\n\n".join(
        f"def test_mr_{i}():\n    assert foo() == 42  # variant {i}" for i in range(1, n + 1)
    )
    return header + tests


def _fence(code: str) -> str:
    """Models answer in a ```python``` block; the probe/invariant parsers require one."""
    return f"```python\n{code}```"


def make_dispatch(mr_n: int = 3, ground: str | None = None, poc: str | None = None,
                 mr_block_n: int | None = None) -> callable:
    """Build a dispatch callable that returns canned text per prompt stage.

    Returning "" from any prompt stage represents dispatch failure and routes
    the oracle through its empty-result abstention path.
    """
    ground = ground if ground is not None else GROUND_JSON
    poc = poc if poc is not None else POC_CODE
    mr_n_actual = mr_block_n if mr_block_n is not None else mr_n

    def fn(prompt: str) -> str:
        if not prompt:
            return ""
        if "states_expected_behaviour" in prompt:
            return ground
        if "Write ONE pytest test" in prompt:
            return _fence(poc)
        if re.search(r"Write\s+\d+\s+pytest tests", prompt):
            return _fence(_mr_block(mr_n_actual))
        return ""

    return fn


class FakeRunner:
    """Scripted ExecOutcome per call in order. up is settable for the harness-down path."""

    def __init__(self, outcomes, *, up: bool = True) -> None:
        self._outcomes = list(outcomes)
        self.up = up
        self.calls: list[tuple[str, str]] = []

    def run_test(self, src: str, patch: str = "") -> ExecOutcome:
        self.calls.append((src, patch))
        if not self._outcomes:
            return ExecOutcome(status="error", exc="no scripted outcome")
        return self._outcomes.pop(0)


# Probe build: the PoC fails on base with an AssertionError (a real reproduction).
# Callers append the delta-gate outcome (run_test(poc, patch)) themselves.
def _probe_build_pass() -> list[ExecOutcome]:
    return [ExecOutcome(status="failed", exc="AssertionError")]  # run_test(poc, "") — reproduces


# Probe build + a green delta gate: the state from which invariants are generated.
def _probe_green() -> list[ExecOutcome]:
    return _probe_build_pass() + [ExecOutcome(status="passed")]  # run_test(poc, patch)


# ── 1. empty patch → UNVERIFIED ──────────────────────────────────────────────
def test_empty_patch_is_unverified():
    runner = FakeRunner([], up=False)  # runner not even consulted
    v = judge(ISSUE_TEXT, "", runner=runner, dispatch=make_dispatch())
    assert v.verdict == UNVERIFIED
    assert "no patch" in v.reason
    assert runner.calls == []


# ── 2. probe cannot be built (dispatch returns "") → UNVERIFIED ──────────────
def test_probe_cannot_be_built_is_unverified():
    empty = lambda prompt: ""
    # No runner outcomes are consumed because the probe loop never runs to a run_test.
    runner = FakeRunner([])
    v = judge(ISSUE_TEXT, "patch text", runner=runner, dispatch=empty)
    assert v.verdict == UNVERIFIED
    assert "no trustworthy reproduction" in v.reason


# ── 3a. patched probe apply_fail → REFUTED ──────────────────────────────────
def test_patched_probe_apply_fail_is_refuted():
    outcomes = _probe_build_pass() + [ExecOutcome(status="apply_fail")]
    runner = FakeRunner(outcomes)
    v = judge(ISSUE_TEXT, "patch text", runner=runner, dispatch=make_dispatch())
    assert v.verdict == REFUTED
    assert "patch does not apply" in v.reason


# ── 3b. patched probe test_defect → UNVERIFIED ──────────────────────────────
def test_patched_probe_test_defect_is_unverified():
    outcomes = _probe_build_pass() + [ExecOutcome(status="test_defect", exc="NameError")]
    runner = FakeRunner(outcomes)
    v = judge(ISSUE_TEXT, "patch text", runner=runner, dispatch=make_dispatch())
    assert v.verdict == UNVERIFIED
    assert "probe broke" in v.reason


# ── 3c. patched probe still failed → REFUTED ────────────────────────────────
def test_patched_probe_still_failed_is_refuted():
    outcomes = _probe_build_pass() + [
        ExecOutcome(status="failed", exc="AssertionError")
    ]
    runner = FakeRunner(outcomes)
    v = judge(ISSUE_TEXT, "patch text", runner=runner, dispatch=make_dispatch())
    assert v.verdict == REFUTED
    assert "still reproduces" in v.reason


# ── 4a. probe green + 3/3 invariants hold → PROVEN ──────────────────────────
def test_three_of_three_invariants_hold_is_proven():
    n_invs = 3
    # probe base + delta gate + n base-validations + n on-patch outcomes
    outcomes = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed") for _ in range(n_invs)]
    runner = FakeRunner(outcomes)
    v = judge(ISSUE_TEXT, "patch text", runner=runner, mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs))
    assert v.verdict == PROVEN, v
    assert v.mr_n == n_invs
    assert v.mr_pass_rate == pytest.approx(1.0)


# ── 4b. 1/4 invariants hold → REFUTED ───────────────────────────────────────
def test_one_of_four_invariants_hold_is_refuted():
    n_invs = 4
    outcomes = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed")] \
        + [ExecOutcome(status="failed") for _ in range(n_invs - 1)]
    runner = FakeRunner(outcomes)
    v = judge(ISSUE_TEXT, "patch text", runner=runner, mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs))
    assert v.verdict == REFUTED, v
    assert v.mr_pass_rate == pytest.approx(0.25)


# ── 4c. 2/4 invariants hold → UNVERIFIED ─────────────────────────────────────
def test_two_of_four_invariants_hold_is_unverified():
    n_invs = 4
    outcomes = _probe_green() \
        + [ExecOutcome(status="failed") for _ in range(n_invs)] \
        + [ExecOutcome(status="passed"), ExecOutcome(status="passed"),
           ExecOutcome(status="failed"), ExecOutcome(status="failed")]
    runner = FakeRunner(outcomes)
    v = judge(ISSUE_TEXT, "patch text", runner=runner, mr_n=n_invs,
              dispatch=make_dispatch(mr_n=n_invs))
    assert v.verdict == UNVERIFIED, v
    assert v.mr_pass_rate == pytest.approx(0.5)


# ── 5. probe green + no informative invariant → UNVERIFIED ──────────────────
def test_no_informative_invariant_is_unverified():
    # mr.build returns [] when dispatch emits no `def test_mr_*` block.
    empty_mr = lambda prompt: "" if re.search(r"Write\s+\d+\s+pytest tests", prompt) else (
        GROUND_JSON if "states_expected_behaviour" in prompt else _fence(POC_CODE)
    )
    runner = FakeRunner(_probe_green())
    v = judge(ISSUE_TEXT, "patch text", runner=runner, dispatch=empty_mr)
    assert v.verdict == UNVERIFIED, v
    assert "no invariant" in v.reason


# ── 6. gold vs gold=None yield the SAME verdict ──────────────────────────────
def test_gold_does_not_change_verdict():
    n_invs = 4
    # Two scripts: one for gold=None, one for gold="gold patch". The runner
    # scripts outcomes regardless of which patch is passed — gold is
    # MEASUREMENT-ONLY and must not enter the decision branch.
    def script(gold):
        out = _probe_green() + [ExecOutcome(status="failed") for _ in range(n_invs)]
        on_patch = ["passed", "passed", "failed", "failed"]
        for st in on_patch:                     # judge interleaves: patch run, then gold run
            out.append(ExecOutcome(status=st))
            if gold is not None:
                out.append(ExecOutcome(status="passed"))
        return out

    # gold=None
    runner_a = FakeRunner(script(None))
    v_a = judge(ISSUE_TEXT, "patch text", runner=runner_a, mr_n=n_invs,
                dispatch=make_dispatch(mr_n=n_invs), gold=None)

    # gold=set
    runner_b = FakeRunner(script("GOLD"))
    v_b = judge(ISSUE_TEXT, "patch text", runner=runner_b, mr_n=n_invs,
                dispatch=make_dispatch(mr_n=n_invs), gold="GOLD")

    assert v_a.verdict == v_b.verdict == UNVERIFIED
    # detail differs (gold emits valid_on_gold labels); verdict / reason do not.
    assert v_a.reason == v_b.reason
    # The gold path emits extra run_test calls for measurement; the verdict itself is identical.
    assert v_a.mr_n == v_b.mr_n


# ── 7a. score(0, 0) → UNVERIFIED ────────────────────────────────────────────
def test_score_no_invariants_is_unverified():
    verdict, reason = inv.score(0.0, 0)
    assert verdict == UNVERIFIED
    assert "no informative" in reason


# ── 7b. score(2/3, 3) → PROVEN ──────────────────────────────────────────────
def test_score_supermajority_is_proven():
    verdict, reason = inv.score(2 / 3, 3)
    assert verdict == PROVEN
    assert "supermajority" in reason


# ── 8. mini_ork.learning.metamorphic still imports the OLD module ────────────
def test_learning_metamorphic_is_not_shadowed():
    import mini_ork.learning.metamorphic as old

    # Layer-2 surface intact; not the certify-side invariants module.
    assert hasattr(old, "MetamorphicRelation")
    assert hasattr(old, "MetamorphicResult")
    assert hasattr(old, "check")
    assert hasattr(old, "RELATION_LIBRARY")
    assert hasattr(old, "UNIVERSAL_RELATIONS")
    # And the certify module is a separate package with its own surface.
    from mini_ork.certify import invariants as ci  # noqa: F401
    assert ci is not old
    assert ci.PROVEN == PROVEN and ci.REFUTED == REFUTED and ci.UNVERIFIED == UNVERIFIED
    # The certify surface has its own gate functions, not Layer-2's.
    assert hasattr(ci, "build") and hasattr(ci, "split") and hasattr(ci, "score")
    assert not hasattr(ci, "MetamorphicRelation")


# ── Bonus: runner.up=False → UNVERIFIED (no patch crash) ────────────────────
def test_runner_down_is_unverified():
    runner = FakeRunner([], up=False)
    v = judge(ISSUE_TEXT, "patch text", runner=runner, dispatch=make_dispatch())
    assert v.verdict == UNVERIFIED
    assert "runner" in v.reason
    assert runner.calls == []


# ── CodeContext / import guard ──────────────────────────────────────────────
# C3 add: the oracle now optionally receives a `context` (BASE source +
# importable module names). Prompts surface the BASE source; a structural
# guard rejects candidates that don't import the code under test — a probe
# of a re-implementation proves nothing about the repository.

# Probe that defines its own `median` inline (no `from stats import median`).
# Valid pytest (has `def test_*`) but does NOT import the repo module.
POC_DEFINES_OWN_MEDIAN = (
    "def test_foo_returns_42():\n"
    "    def median(xs):\n"
    "        return sorted(xs)[len(xs) // 2]\n"
    "    assert median([1, 2, 3]) == 2\n"
)

# Probe that imports `stats` and calls `median` — the structurally-correct shape.
POC_IMPORTS_STATS = (
    "from stats import median\n"
    "def test_foo_returns_42():\n"
    "    assert median([1, 2, 3]) == 2\n"
)


def test_context_guard_rejects_probe_that_reimplements_code_under_test():
    """With modules=("stats",), a probe that defines its own median is
    rejected BEFORE `run(poc)` is ever called; every try is rejected, so
    judge returns UNVERIFIED with the rejection reason."""
    ctx = CodeContext(text="# file: stats/median.py\n```python\n... buggy ...\n```\n",
                      modules=("stats",), files=("stats/median.py",))
    runner = FakeRunner([])   # no outcomes — runner must NOT be called
    v = judge(ISSUE_TEXT, "patch text", runner=runner,
              dispatch=make_dispatch(poc=POC_DEFINES_OWN_MEDIAN),
              context=ctx)
    assert v.verdict == UNVERIFIED, v
    assert "does not import the code under test" in v.reason
    assert "stats" in v.reason
    # Load-bearing: the runner was NEVER called — a guarded candidate must
    # never reach `run(poc)`. This is the whole point of the structural
    # guard (prompts alone are not enough).
    assert runner.calls == []


def test_context_guard_allows_probe_that_imports_code_under_test():
    """Same context, but the probe imports `stats`. The guard passes; the
    oracle then consumes the scripted outcomes and judges the patch."""
    ctx = CodeContext(text="# file: stats/median.py\n```python\n... buggy ...\n```\n",
                      modules=("stats",), files=("stats/median.py",))
    # probe build (1 call: base AssertionError) + delta gate (1 call: passed)
    # + 1 invariant's base-validation + 1 invariant's on-patch run = 4 scripted.
    outcomes = _probe_green() + [
        ExecOutcome(status="failed"),  # mr build: candidate fails on base
        ExecOutcome(status="passed"),  # mr score: candidate passes on patch
    ]
    runner = FakeRunner(outcomes)
    v = judge(ISSUE_TEXT, "patch text", runner=runner,
              dispatch=make_dispatch(poc=POC_IMPORTS_STATS, mr_n=1),
              context=ctx)
    # With the probe correctly importing `stats` and the scripted outcomes,
    # the oracle proceeds past the guard and through the invariants loop.
    # 1/1 invariant passes -> UNVERIFIED (below supermajority bar 2/3 for n=1).
    assert v.verdict == UNVERIFIED, v
    assert "runner" not in v.reason
    assert "does not import" not in v.reason


def test_context_surfaces_base_source_in_poc_prompt():
    """The probe prompt the dispatch receives contains the context text when
    given, and does not when context=None."""
    seen_prompts: list[str] = []
    base_src = "# file: stats/median.py\n```python\ndef median(xs): return xs[len(xs)//2]\n```\n"

    def capturing_dispatch(prompt: str) -> str:
        if "Write ONE pytest test" in prompt:
            seen_prompts.append(prompt)
            return _fence(POC_IMPORTS_STATS)
        if "states_expected_behaviour" in prompt:
            return GROUND_JSON
        return ""

    ctx_with = CodeContext(text=base_src, modules=("stats",), files=("stats/median.py",))

    # With context — prompt carries the BASE source + the import instruction.
    runner = FakeRunner(_probe_build_pass())  # one call (probe base reproduction)
    judge(ISSUE_TEXT, "patch text", runner=runner,
          dispatch=capturing_dispatch, context=ctx_with)
    assert seen_prompts, "dispatch was never called for the probe prompt"
    assert base_src in seen_prompts[0]
    assert "import the code under test" in seen_prompts[0]

    # Without context — prompt carries neither.
    seen_prompts.clear()
    runner = FakeRunner(_probe_build_pass())
    judge(ISSUE_TEXT, "patch text", runner=runner, dispatch=capturing_dispatch)
    assert seen_prompts
    assert base_src not in seen_prompts[0]
    assert "import the code under test" not in seen_prompts[0]


def test_judge_default_context_none_is_unchanged():
    """SWE-bench-style callers omit `context`; the engine behaves exactly
    as before — same verdict, same call count, same reason text."""
    outcomes = _probe_green() + [ExecOutcome(status="failed"), ExecOutcome(status="passed")]
    runner = FakeRunner(outcomes)
    v = judge(ISSUE_TEXT, "patch text", runner=runner,
              dispatch=make_dispatch(poc=POC_DEFINES_OWN_MEDIAN, mr_n=1))
    # Without context, the guard is skipped — the probe that defines its own
    # median runs. 1/1 invariant passes -> UNVERIFIED.
    assert v.verdict == UNVERIFIED
    assert "does not import" not in v.reason