"""Unit tests for the execution-evidence coherence predicate.

The predicate (`mini_ork.certify.evidence`) is the one place that decides when a
runner's claimed ``passed`` contradicts its own fields. These tests pin each
rule's exact reason shape, the honest pass that must never be flagged, the
defensive contract (malformed evidence never raises), and the acceptance test
from the kickoff: the shipped judge passes the shipped oracle-integrity audit.

Hermetic: no network, no model, no sandbox. Every outcome is a real
``ExecOutcome`` built in-process.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from mini_ork.certify import evidence
from mini_ork.certify import oracle
from mini_ork.gates import oracle_integrity
from mini_ork.runtime import ExecOutcome


def _outcome(**fields) -> ExecOutcome:
    """A real ExecOutcome; the defaults are the honest-pass shape."""
    base = {
        "status": "passed",
        "ran": True,
        "returncode": 0,
        "output": "1 passed",
        "backend": "scripted",
        "exc": "",
        "meta": {},
    }
    base.update(fields)
    return ExecOutcome(**base)


# ── acceptance: the shipped judge passes the shipped audit ────────────────────


def test_shipped_judge_passes_the_shipped_audit():
    result = oracle_integrity.run_default_audit()
    assert result["controls_failed"] == []
    assert result["missed"] == []
    assert result["pass"] is True


# ── the honest path ──────────────────────────────────────────────────────────


def test_an_honest_pass_has_no_defect():
    honest = _outcome(status="passed", ran=True, returncode=0, meta={})
    assert evidence.defect(honest) is None
    assert evidence.is_trustworthy_pass(honest) is True


@pytest.mark.parametrize(
    "outcome",
    [
        _outcome(),
        _outcome(ran=False),
        _outcome(returncode=None),
        _outcome(meta={"apply_status": "ok"}),
        _outcome(meta={"attempts": ["error", "ok"]}),
        _outcome(meta={"executed": 3}),
        _outcome(meta={"test_defect": False}),
        _outcome(meta={"input_digest": "d", "candidate_input_digest": "d"}),
    ],
)
def test_is_trustworthy_pass_mirrors_defect_is_none(outcome):
    assert evidence.is_trustworthy_pass(outcome) == (evidence.defect(outcome) is None)


# ── a non-passed status is never re-labelled ─────────────────────────────────


@pytest.mark.parametrize("status", ["failed", "error", "test_defect", "apply_fail", "no_run"])
def test_a_non_passed_status_is_never_relabelled(status):
    contradictory = _outcome(
        status=status,
        ran=False,
        returncode=1,
        meta={"apply_status": "apply_fail", "final_attempt_status": "error",
              "attempts": ["error"], "executed": 0, "test_defect": True},
    )
    assert evidence.defect(contradictory) is None


# ── each rule, with its exact reason shape ───────────────────────────────────


def test_rule_1_ran_is_false():
    # The caller POPULATED run evidence (returncode=0), so `ran is False`
    # contradicts it.
    assert evidence.defect(_outcome(ran=False, returncode=0)) == (
        "the outcome claims passed but ran is False"
    )


def test_rule_1_a_bare_default_pass_is_not_flagged():
    # `ExecOutcome.ran` defaults to False, so a caller that populated no run
    # evidence at all (returncode None, empty meta) has nothing to contradict.
    bare = ExecOutcome(status="passed")
    assert bare.ran is False and bare.returncode is None and bare.meta == {}
    assert evidence.defect(bare) is None


@pytest.mark.parametrize("returncode", [1, 2, -1])
def test_rule_2_nonzero_returncode(returncode):
    assert evidence.defect(_outcome(returncode=returncode)) == (
        f"the outcome claims passed but returncode is {returncode!r}"
    )


def test_rule_2_a_none_returncode_is_unrecorded_not_a_failure():
    assert evidence.defect(_outcome(returncode=None)) is None


def test_rule_3_apply_status_not_a_success_word():
    assert evidence.defect(_outcome(meta={"apply_status": "apply_fail"})) == (
        "the outcome claims passed but meta['apply_status'] is "
        "'apply_fail', not a success word"
    )


@pytest.mark.parametrize(
    "word", ["ok", "passed", "pass", "applied", "clean", "success", "succeeded"]
)
def test_rule_3_and_4_exact_success_words_are_coherent(word):
    assert evidence.defect(_outcome(meta={"apply_status": word})) is None
    assert evidence.defect(_outcome(meta={"final_attempt_status": word})) is None


@pytest.mark.parametrize("near_miss", ["ok.", "successful", "applied!"])
def test_rule_3_a_near_miss_is_still_a_defect(near_miss):
    assert evidence.defect(_outcome(meta={"apply_status": near_miss})) is not None


def test_rule_4_final_attempt_status_not_a_success_word():
    assert evidence.defect(_outcome(meta={"final_attempt_status": "error"})) == (
        "the outcome claims passed but meta['final_attempt_status'] is "
        "'error', not a success word"
    )


def test_rule_5_attempts_without_a_success_entry():
    expected = "the outcome claims passed but no attempt in meta['attempts'] succeeded"
    assert evidence.defect(_outcome(meta={"attempts": ["error", "error"]})) == expected
    assert evidence.defect(_outcome(meta={"attempts": ("error",)})) == expected
    # one corroborating attempt, an empty sequence, and a non-sequence all say
    # nothing contradictory about the aggregate.
    assert evidence.defect(_outcome(meta={"attempts": ["error", "ok"]})) is None
    assert evidence.defect(_outcome(meta={"attempts": []})) is None
    assert evidence.defect(_outcome(meta={"attempts": 5})) is None


def test_rule_6_executed_zero():
    assert evidence.defect(_outcome(meta={"executed": 0})) == (
        "the outcome claims passed but meta['executed'] is 0"
    )
    assert evidence.defect(_outcome(meta={"executed": 3})) is None


def test_rule_7_truthy_test_defect():
    assert evidence.defect(_outcome(meta={"test_defect": True})) == (
        "the outcome claims passed but meta['test_defect'] is truthy"
    )
    assert evidence.defect(_outcome(meta={"test_defect": False})) is None


def test_rule_8_differing_input_digests():
    replayed = _outcome(meta={
        "input_digest": "sha256:" + "0" * 64,
        "candidate_input_digest": "sha256:" + "f" * 64,
    })
    assert evidence.defect(replayed) == (
        "the outcome claims passed but the evidence was computed for a "
        "different input digest"
    )


def test_rule_8_matching_or_incomplete_digests_are_coherent():
    same = "sha256:" + "0" * 64
    assert evidence.defect(
        _outcome(meta={"input_digest": same, "candidate_input_digest": same})
    ) is None
    assert evidence.defect(_outcome(meta={"input_digest": same})) is None
    assert evidence.defect(
        _outcome(meta={"input_digest": "", "candidate_input_digest": same})
    ) is None


def test_the_reason_is_a_non_empty_string():
    reason = evidence.defect(_outcome(returncode=1))
    assert isinstance(reason, str) and reason.strip()


# ── defensive: malformed evidence never raises ──────────────────────────────


@pytest.mark.parametrize(
    "outcome",
    [SimpleNamespace(), SimpleNamespace(status="passed"), object()],
)
def test_missing_attributes_never_raise(outcome):
    result = evidence.defect(outcome)
    assert result is None or isinstance(result, str)


@pytest.mark.parametrize("meta", [None, "not a mapping", 42, 3.5, ["a", "b"], object()])
def test_a_non_mapping_meta_never_raises(meta):
    result = evidence.defect(_outcome(meta=meta))
    assert result is None or isinstance(result, str)


def test_unknown_meta_values_never_raise():
    outcome = _outcome(meta={
        "apply_status": 7,
        "final_attempt_status": object(),
        "attempts": 5,
        "executed": "many",
        "test_defect": "no",
        "input_digest": 1,
        "candidate_input_digest": 2,
    })
    result = evidence.defect(outcome)
    assert result is None or isinstance(result, str)


# ── the module stays stdlib-only at import time ─────────────────────────────


def test_the_module_imports_only_the_stdlib_at_module_scope():
    import ast
    import sys
    from pathlib import Path

    source = Path(evidence.__file__).read_text(encoding="utf-8")
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    roots = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add("(relative import)")
            else:
                roots.add((node.module or "").split(".")[0])
    assert sorted(root for root in roots if root not in stdlib) == []


# ── wiring sanity: oracle uses the predicate, not a lookup table ────────────


def test_the_oracle_imports_the_predicate():
    assert oracle.evidence is evidence


# ── the metamorphic loop withholds a PROVEN built on incoherent passes ──────

_ISSUE = (
    "The bug is: add(1, 2) returns 4 instead of the expected 3. "
    "The documented behaviour is that add(a, b) returns a + b."
)
_PATCH = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
_POC = "def test_add():\n    assert add(1, 2) == 3\n"


class _IncoherentInvariantRunner:
    """Honest base/delta evidence; listed invariants come back as forged passes."""

    up = True

    def __init__(self, incoherent: set[int] | None = None) -> None:
        # Default: every invariant lies. A caller can let some of them pass
        # coherently to pin the mixed measured/unmeasured accounting.
        self.incoherent = {1, 2, 3} if incoherent is None else incoherent

    def run_test(self, src: str, patch: str = "") -> ExecOutcome:
        passed = _outcome()
        failed = ExecOutcome(
            status="failed", ran=True, returncode=1, exc="AssertionError",
            output="E   AssertionError: assert 4 == 3",
        )
        hit = re.search(r"def (test_mr_\d+)", src)
        if hit:
            if not patch:
                return failed                      # informative on base
            index = int(hit.group(1).rsplit("_", 1)[1])
            if index in self.incoherent:
                # Claims a pass while its bookkeeping says every attempt errored.
                return _outcome(meta={"final_attempt_status": "error"})
            return passed
        return passed if patch else failed


def _mr_dispatch(prompt: str) -> str:
    if re.search(r"Write\s+\d+\s+pytest tests", prompt or ""):
        body = "\n\n".join(
            f"def test_mr_{i}():\n    assert add(2, 3) == 5" for i in range(1, 4)
        )
        return f"```python\n{body}\n```"
    return ""


def test_metamorphic_loop_withholds_proven_on_incoherent_passes():
    v = oracle.judge(
        _ISSUE, _PATCH, runner=_IncoherentInvariantRunner(),
        dispatch=_mr_dispatch, poc=_POC,
    )
    assert v.verdict == "UNVERIFIED"
    assert v.detail["unmeasured"] >= 1
    assert any(rec.get("outcome") == "unmeasured" for rec in v.detail["invariants"])
    # All-unmeasured evidence scores on an empty denominator, not 0 of 3.
    assert v.mr_n == v.detail["n_effective"] == 0
    assert v.mr_pass_rate is None


def test_measured_denominator_pairs_mr_pass_rate_with_mr_n():
    # Two invariants measure cleanly and hold; the third's pass is forged. The
    # withheld PROVEN must report the pair it actually scored on (2 of 2), not
    # a rate over the 3 trials one of which measured nothing.
    v = oracle.judge(
        _ISSUE, _PATCH, runner=_IncoherentInvariantRunner(incoherent={3}),
        dispatch=_mr_dispatch, poc=_POC,
    )
    assert v.verdict == "UNVERIFIED"
    assert v.detail["unmeasured"] == 1
    assert v.detail["invariants"] and v.detail["n_effective"] == 2
    assert v.mr_n == 2
    assert v.mr_pass_rate == 1.0
