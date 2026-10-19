"""Unit tests for the execution-anchored recompute surface (kickoff tests 1-8)."""

from __future__ import annotations

import importlib
import json
import random
import uuid

import pytest

from mini_ork.certify.recompute import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    RecomputeResult,
    check,
    replay,
)
from mini_ork.verify import behavioral


def _adder(state, record):
    """Deterministic, pure apply function: fold records into a running sum."""
    return {"v": (state or {}).get("v", 0) + record}


def _canonical_side(value):
    """Normalize a witness side to sorted-key JSON text."""
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(value, sort_keys=True)


# --------------------------------------------------------------------------- #
# Test 1: deterministic apply + 3 records
# --------------------------------------------------------------------------- #
def test_deterministic_replay_is_proven_and_reports_every_step():
    states = replay(_adder, [1, 2, 3], initial_state={"v": 0})
    assert len(states) == 3

    result = check(_adder, [1, 2, 3], initial_state={"v": 0})
    assert result.status == PROVEN
    assert isinstance(result, RecomputeResult)
    assert result.witness is None
    assert all({"name", "ok", "detail"} <= set(entry) for entry in result.checks)


def test_replay_deep_copies_the_callers_initial_state():
    initial = {"v": 0}
    assert replay(_adder, [1, 2], initial_state=initial) == [{"v": 1}, {"v": 3}]
    assert initial == {"v": 0}


def test_check_runs_two_independent_replays():
    def mutating(state, record):
        state["log"].append(record)
        return state

    initial = {"log": []}
    result = check(mutating, ["a", "b"], initial_state=initial)
    assert result.status == PROVEN
    assert initial == {"log": []}


# --------------------------------------------------------------------------- #
# Test 2: non-deterministic apply -> REFUTED with a canonical witness
# --------------------------------------------------------------------------- #
def test_nondeterministic_apply_is_refuted_with_a_canonical_witness():
    random.seed(0xBEEF)

    def flaky(state, record):
        return {"v": (state or {}).get("v", 0) + record + random.random()}

    result = check(flaky, [1, 2, 3], initial_state={})
    assert result.status == REFUTED
    witness = result.witness
    assert witness is not None
    assert {"index", "left", "right"} <= set(witness)
    assert isinstance(witness["index"], int)
    assert 0 <= witness["index"] <= 2
    for side in ("left", "right"):
        normalized = _canonical_side(witness[side])
        assert json.dumps(json.loads(normalized), sort_keys=True) == normalized


# --------------------------------------------------------------------------- #
# Test 3: expectation mismatch -> REFUTED with the final-state witness
# --------------------------------------------------------------------------- #
def test_expected_state_mismatch_is_refuted_and_names_both_finals():
    records = [1, 2, 3]
    result = check(
        _adder,
        records,
        initial_state={},
        has_expected=True,
        expected_state={"v": 999},
    )
    assert result.status == REFUTED
    witness = result.witness
    assert witness is not None
    assert witness["index"] == len(records) - 1
    assert {_canonical_side(witness["left"]), _canonical_side(witness["right"])} == {
        _canonical_side({"v": 999}),
        _canonical_side({"v": 6}),
    }


# --------------------------------------------------------------------------- #
# Test 4: key reordering is irrelevant under exact canonical equality
# --------------------------------------------------------------------------- #
def test_expected_state_match_under_dict_key_reordering_is_proven():
    def apply_fn(state, record):
        return {"a": record, "b": record * 2}

    result = check(
        apply_fn,
        [1],
        has_expected=True,
        expected_state={"b": 2, "a": 1},
        equivalence="exact",
    )
    assert result.status == PROVEN


def test_expected_state_is_ignored_unless_has_expected():
    result = check(_adder, [1, 2, 3], initial_state={}, expected_state={"v": 999})
    assert result.status == PROVEN


# --------------------------------------------------------------------------- #
# Test 5: empty records
# --------------------------------------------------------------------------- #
def test_empty_records_are_unverified():
    result = check(_adder, [], initial_state={}, has_expected=True, expected_state={"v": 0})
    assert result.status == UNVERIFIED
    assert "no recompute records declared" in result.evidence + json.dumps(result.checks)


# --------------------------------------------------------------------------- #
# Test 6: a raising apply is a harness failure, never a refutation
# --------------------------------------------------------------------------- #
def test_apply_fn_raising_is_unverified_never_refuted():
    def boom(state, record):
        raise RuntimeError("boom-apply-fn")

    result = check(boom, [1, 2], initial_state={})
    assert result.status == UNVERIFIED
    assert result.status != REFUTED
    assert "boom-apply-fn" in result.evidence + json.dumps(result.checks)


# --------------------------------------------------------------------------- #
# Test 7: opaque final state cannot be compared -> UNVERIFIED
# --------------------------------------------------------------------------- #
def test_non_json_final_state_is_unverified():
    def opaque(state, record):
        return {"inner": object()}

    result = check(opaque, [1], initial_state={})
    assert result.status == UNVERIFIED
    assert result.status != REFUTED


def test_unserializable_set_final_state_is_unverified():
    def with_set(state, record):
        return {"members": {1, 2, 3}}

    result = check(with_set, [1], initial_state={})
    assert result.status == UNVERIFIED
    assert result.status != REFUTED


# --------------------------------------------------------------------------- #
# Equivalence operators delegate to the shipped implementation
# --------------------------------------------------------------------------- #
def test_unknown_equivalence_label_is_unverified_naming_the_label():
    result = check(
        _adder,
        [1, 2],
        initial_state={},
        has_expected=True,
        expected_state={"v": 3},
        equivalence="definitely-not-an-operator",
    )
    assert result.status == UNVERIFIED
    assert "definitely-not-an-operator" in result.evidence + json.dumps(result.checks)


def test_set_equivalence_delegates_to_the_shipped_operator():
    def apply_fn(state, record):
        return {"items": [3, 1, 2]}

    expected = {"items": [1, 2, 3]}
    exact_result = check(
        apply_fn, [{}], has_expected=True, expected_state=expected, equivalence="exact"
    )
    assert exact_result.status == REFUTED
    set_result = check(
        apply_fn, [{}], has_expected=True, expected_state=expected, equivalence="set"
    )
    assert set_result.status == PROVEN


def test_tolerant_without_rules_abstains():
    def apply_fn(state, record):
        return {"v": 0.1000001}

    result = check(
        apply_fn,
        [{}],
        has_expected=True,
        expected_state={"v": 0.1},
        equivalence="tolerant",
    )
    assert result.status == UNVERIFIED


# --------------------------------------------------------------------------- #
# Test 8: behavioral integration over the observable-spec path
# --------------------------------------------------------------------------- #
ADDER_SUBJECT = '''\
"""Test subject: a deterministic apply function."""
def apply(state, record):
    return {"v": (state or {}).get("v", 0) + record["add"]}
'''


def _install_subject(monkeypatch, tmp_path, source):
    name = "recompute_subject_" + uuid.uuid4().hex
    (tmp_path / (name + ".py")).write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    return name


def _observable(**overrides):
    mapping = {
        "surface": "recompute",
        "module": "some_module",
        "function": "apply",
        "recompute": {"records": [1, 2]},
    }
    mapping.update(overrides)
    return behavioral.Observable.from_mapping(mapping)


def test_run_routes_recompute_and_proves_a_matching_expectation(monkeypatch, tmp_path):
    name = _install_subject(monkeypatch, tmp_path, ADDER_SUBJECT)
    records = [{"add": 1}, {"add": 2}, {"add": 3}]
    obs = behavioral.Observable.from_mapping(
        {
            "surface": "recompute",
            "module": name,
            "function": "apply",
            "recompute": {"records": records, "expect_state": {"v": 6}},
        }
    )
    verdict = behavioral.run(obs)
    assert verdict.status == behavioral.PROVEN

    # One behavioral Check per recompute check dict, evidence copied verbatim.
    from mini_ork.certify import recompute as recompute_mod

    module = importlib.import_module(name)
    expected = recompute_mod.check(
        module.apply, records, has_expected=True, expected_state={"v": 6}
    )
    assert verdict.evidence == expected.evidence
    assert len(verdict.checks) == len(expected.checks)
    assert all(isinstance(c, behavioral.Check) for c in verdict.checks)
    for verdict_check, check_dict in zip(verdict.checks, expected.checks):
        assert verdict_check.name == check_dict["name"]
        assert verdict_check.ok == check_dict["ok"]


def test_run_mismatching_expectation_is_refuted(monkeypatch, tmp_path):
    name = _install_subject(monkeypatch, tmp_path, ADDER_SUBJECT)
    obs = behavioral.Observable.from_mapping(
        {
            "surface": "recompute",
            "module": name,
            "function": "apply",
            "recompute": {
                "records": [{"add": 1}, {"add": 2}, {"add": 3}],
                "expect_state": {"v": 999},
            },
        }
    )
    assert behavioral.run(obs).status == behavioral.REFUTED


def test_observable_rejects_malformed_recompute_blocks():
    for bad in (
        ["not", "a", "mapping"],
        {"no_records_key": []},
        {"records": "not-a-list"},
        {"records": {"a": 1}},
    ):
        with pytest.raises(behavioral.ObservableError):
            behavioral.Observable.from_mapping(
                {
                    "surface": "recompute",
                    "module": "some_module",
                    "function": "apply",
                    "recompute": bad,
                }
            )


def test_observable_recompute_defaults_to_a_fresh_empty_dict():
    first = behavioral.Observable.from_mapping(
        {"surface": "api", "target": "https://example.test/x"}
    )
    second = behavioral.Observable.from_mapping(
        {"surface": "api", "target": "https://example.test/y"}
    )
    assert first.recompute == {}
    assert first.recompute is not second.recompute


def test_recompute_surface_is_registered_and_ordered_last():
    assert behavioral._SURFACES == ("api", "ui", "journey", "function", "recompute")
    assert behavioral.get_surface_handler("recompute") is behavioral.run_recompute_check


def test_run_unresolvable_subject_abstains_never_raises():
    obs = _observable(module="recompute_no_such_module_zz")
    verdict = behavioral.run(obs)
    assert verdict.status == behavioral.UNVERIFIED
    text = verdict.evidence + "".join(c.detail for c in verdict.checks)
    assert "recompute_no_such_module_zz" in text
