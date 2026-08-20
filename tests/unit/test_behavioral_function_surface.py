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
        lambda module, function, fn: ["determinism", "; rm -rf /"],
    )
    monkeypatch.setenv("MO_BEHAV_FN_PROPOSE", "1")

    verdict = run_function_check(_observable("_deterministic", relations=[]))

    assert verdict.status == PROVEN
    assert seen == ["determinism"]


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
