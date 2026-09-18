"""Function-surface wiring for the behavioral metamorphic verifier."""

from __future__ import annotations

import json

import pytest

from mini_ork.learning import metamorphic as mm
from mini_ork.verify import behavioral
from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    Observable,
    ObservableError,
    get_surface_handler,
    observable_from_env,
    run_function_check,
)
from mini_ork.verify.reward import verdict_reward


_STATE = 0


def _deterministic(value):
    return value * 2


def _stateful(value):
    global _STATE
    _STATE += 1
    return value + _STATE


def _mutating(values):
    values.append("changed")
    return len(values)


def _observable(function: str, *, seeds=None, relations=None) -> Observable:
    return Observable.from_mapping(
        {
            "surface": "function",
            "module": __name__,
            "function": function,
            "seed_inputs": [2] if seeds is None else seeds,
            "relations": ["determinism"] if relations is None else relations,
        }
    )


def test_deterministic_function_is_proven_and_rewarded():
    verdict = run_function_check(_observable("_deterministic"))

    assert verdict.status == PROVEN
    assert verdict_reward(verdict.status) == 1.0


def test_nondeterministic_function_is_refuted_with_counterexample():
    global _STATE
    _STATE = 0

    verdict = run_function_check(_observable("_stateful"))

    assert verdict.status == REFUTED
    failing = next(check for check in verdict.checks if check.name == "determinism")
    assert failing.ok is False
    assert "counterexamples=" in failing.detail
    assert verdict_reward(verdict.status) == 0.0


def test_input_mutation_is_refuted_by_immutability_check():
    verdict = run_function_check(_observable("_mutating", seeds=[[1]]))

    assert verdict.status == REFUTED
    mutation = next(
        check for check in verdict.checks if check.name == "input_immutability"
    )
    assert mutation.ok is False


def test_missing_seeds_or_target_abstains_without_reward():
    empty = run_function_check(_observable("_deterministic", seeds=[]))
    missing_module = run_function_check(
        Observable.from_mapping(
            {
                "surface": "function",
                "module": "does_not_exist_for_mini_ork_tests",
                "function": "fn",
                "seed_inputs": [1],
                "relations": ["determinism"],
            }
        )
    )
    missing_function = run_function_check(_observable("does_not_exist"))

    assert empty.status == UNVERIFIED
    assert verdict_reward(empty.status) is None
    assert missing_module.status == UNVERIFIED
    assert missing_function.status == UNVERIFIED


def test_safe_whitelist_rejects_descriptors_and_filters_proposals(monkeypatch):
    with pytest.raises(ObservableError, match="unknown function relation"):
        _observable("_deterministic", relations=["; rm -rf /"])
    with pytest.raises(ObservableError, match="plain JSON data"):
        _observable("_deterministic", seeds=[object()])

    seen: list[str] = []
    real_check = mm.check

    def recording_check(fn, seed_inputs, relations, **kwargs):
        seen.extend(relation.name for relation in relations)
        return real_check(fn, seed_inputs, relations, **kwargs)

    monkeypatch.setattr(mm, "check", recording_check)
    monkeypatch.setattr(
        behavioral,
        "_propose_relations",
        lambda module, function, fn: {
            "target": {"module": module, "function": function},
            "seed_inputs": [2],
            "relations": ["determinism", "; rm -rf /"],
        },
    )
    monkeypatch.setenv("MO_BEHAV_FN_PROPOSE", "1")

    verdict = run_function_check(_observable("_deterministic", relations=[]))

    assert verdict.status == PROVEN
    assert seen == ["determinism"]


def test_proposed_seeds_anchor_the_check_when_the_recipe_declares_none(monkeypatch):
    """The proposer's seed inputs are used, not discarded.

    A relation with nothing to transform cannot be exercised, so a proposal that
    named relations and carried no anchor would resolve to an abstention the run
    could never get past. Adopting the seeds is what makes the opt-in propose
    path reach a real measurement.
    """
    seen: list = []
    real_check = mm.check

    def recording_check(fn, seed_inputs, relations, **kwargs):
        seen.extend(seed_inputs)
        return real_check(fn, seed_inputs, relations, **kwargs)

    monkeypatch.setattr(mm, "check", recording_check)
    monkeypatch.setattr(
        behavioral,
        "_propose_relations",
        lambda module, function, fn: {
            "target": {"module": module, "function": function},
            "seed_inputs": [7, 9],
            "relations": ["determinism"],
        },
    )
    monkeypatch.setenv("MO_BEHAV_FN_PROPOSE", "1")

    verdict = run_function_check(
        _observable("_deterministic", seeds=[], relations=[]))

    assert verdict.status == PROVEN
    assert seen == [7, 9]
    # ...and the verdict says where the anchor came from, without letting that
    # note move it (the check is True, and `_resolve` reads only False/None).
    source = [c for c in verdict.checks if c.name == "seed_source"]
    assert len(source) == 1 and source[0].ok is True


def test_the_real_proposer_body_reaches_to_spec(monkeypatch):
    """`to_spec` is genuinely called, not merely importable.

    Everything above stubs ``_propose_relations`` away, which proves the caller
    reads a spec but not that a spec is ever built. Here only the model dispatch
    is stubbed, so ``inspect.getsource`` → ``build_proposer_prompt`` →
    ``parse_proposal`` → ``to_spec`` all run for real and the spec they produce
    is what anchors the check. Without this, ``to_spec`` could lose its last
    caller again and the suite would not notice.
    """
    import mini_ork.dispatch as mo_dispatch

    seen_prompt: list[str] = []

    class _Result:
        ok = True
        text = ('```json\n{"relations": ["determinism", "not-a-real-relation"], '
                '"seed_inputs": [4]}\n```')

    def fake_dispatch(request):
        seen_prompt.append(request.prompt)
        return _Result()

    monkeypatch.setattr(mo_dispatch, "dispatch_model", fake_dispatch)
    monkeypatch.setenv("MO_BEHAV_FN_PROPOSE", "1")

    seen_seeds: list = []
    real_check = mm.check

    def recording_check(fn, seed_inputs, relations, **kwargs):
        seen_seeds.extend(seed_inputs)
        return real_check(fn, seed_inputs, relations, **kwargs)

    monkeypatch.setattr(mm, "check", recording_check)

    verdict = run_function_check(
        _observable("_deterministic", seeds=[], relations=[]))

    assert seen_prompt and "_deterministic" in seen_prompt[0]
    # The unresolvable name was dropped by the whitelist and the valid one ran.
    assert seen_seeds == [4]
    assert verdict.status == PROVEN


def test_a_declared_seed_outranks_a_proposed_one(monkeypatch):
    """A recipe's own anchor wins; the proposal is a fallback, never an override."""
    seen: list = []
    real_check = mm.check

    def recording_check(fn, seed_inputs, relations, **kwargs):
        seen.extend(seed_inputs)
        return real_check(fn, seed_inputs, relations, **kwargs)

    monkeypatch.setattr(mm, "check", recording_check)
    monkeypatch.setattr(
        behavioral,
        "_propose_relations",
        lambda module, function, fn: {
            "target": {"module": module, "function": function},
            "seed_inputs": [7, 9],
            "relations": ["determinism"],
        },
    )
    monkeypatch.setenv("MO_BEHAV_FN_PROPOSE", "1")

    verdict = run_function_check(
        _observable("_deterministic", seeds=[3], relations=[]))

    assert seen == [3]
    assert not [c for c in verdict.checks if c.name == "seed_source"]


def test_function_verdict_json_and_environment_shape():
    verdict = run_function_check(_observable("_deterministic"))
    payload = json.loads(verdict.to_json())
    from_env = observable_from_env(
        {
            "MO_BEHAV_SURFACE": "function",
            "MO_BEHAV_MODULE": __name__,
            "MO_BEHAV_FUNCTION": "_deterministic",
            "MO_BEHAV_SEED_INPUTS": "[1, 2]",
            "MO_BEHAV_RELATIONS": "determinism",
        }
    )

    assert payload["verifier"] == "behavioral"
    assert payload["surface"] == "function"
    assert payload["status"] == PROVEN
    assert from_env is not None
    assert from_env.seed_inputs == [1, 2]
    assert from_env.relations == ["determinism"]


def test_function_surface_handler_is_registered():
    assert get_surface_handler("function").__name__ == "run_function_check"
