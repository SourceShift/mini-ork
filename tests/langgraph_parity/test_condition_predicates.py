"""Parity tests for the ``condition:`` predicate evaluator.

These tests pin the AC1 surface of ``p1-1-condition-edges``:

  - ``const``, ``enum``, ``minimum`` / ``maximum``, ``pattern``, ``required``,
    ``allOf`` / ``anyOf`` — the JSON Schema fragments a recipe author can use
    to declare what "fired" means.
  - ``negate: true`` — inverts the predicate sense (fire when match FAILS).
  - Bad pointer (missing key) — handled per F3 (warn, do not fire unless
    ``negate: true``).
  - Non-JSON artifact — handled per F2 (warn, do not fire).
  - Compile-time rejection — unknown keys inside ``condition``, non-string
    pointer, non-dict match (per C2).

The tests live in the ``langgraph_parity`` package because the AC1 gate in
``gates/p1-1-condition-edges.json`` runs pytest against exactly this file;
an accidental rename or move would silently break the gate probe.
"""
from __future__ import annotations

import pytest

from mini_ork.recovery.condition import (
    ConditionSchemaError,
    compile_condition,
    evaluate_condition,
    validate_condition_schema,
)

JSON = '{"verdict": {"passed": false, "reasons": ["flaky", "skipped"]}}'


# ── Predicate firing on JSON Schema fragments ─────────────────────────────────


def test_const_fires_when_resolved_value_matches_constant():
    edge = {
        "condition": {
            "pointer": "/verdict/passed",
            "match": {"const": False},
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, JSON)
    assert outcome.fired is True
    assert outcome.warning == ""


def test_const_does_not_fire_when_resolved_value_differs():
    edge = {
        "condition": {
            "pointer": "/verdict/passed",
            "match": {"const": True},
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, JSON)
    assert outcome.fired is False
    assert "match failed" in outcome.reason


def test_enum_fires_when_resolved_value_in_set():
    edge = {
        "condition": {
            "pointer": "/verdict/passed",
            "match": {"enum": [False, None]},
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, JSON)
    assert outcome.fired is True


def test_minimum_maximum_fires_when_value_in_range():
    # Add a numeric field to the artifact for the range check.
    artifact = '{"duration_ms": 250}'
    edge = {
        "condition": {
            "pointer": "/duration_ms",
            "match": {"minimum": 100, "maximum": 500},
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, artifact)
    assert outcome.fired is True


def test_pattern_fires_when_string_matches_regex():
    artifact = '{"name": "verifier_failed_round_3"}'
    edge = {
        "condition": {
            "pointer": "/name",
            "match": {"pattern": r"^verifier_failed_round_[0-9]+$"},
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, artifact)
    assert outcome.fired is True


def test_required_fires_when_required_key_present():
    # JSON Schema ``required`` lists keys that MUST be present on the
    # resolved object. The verdict object declares ``"passed"`` and
    # ``"reasons"`` — validation succeeds → predicate fires (no negate).
    edge = {
        "condition": {
            "pointer": "/verdict",
            "match": {"required": ["passed", "reasons"]},
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, JSON)
    assert outcome.fired is True


def test_allOf_anyOf_combined_predicates_fire():
    artifact = '{"score": 0.42}'
    edge = {
        "condition": {
            "pointer": "/score",
            "match": {
                "allOf": [
                    {"type": "number"},
                    {"anyOf": [{"minimum": 0.0}, {"maximum": 1.0}]},
                ],
            },
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, artifact)
    assert outcome.fired is True


def test_negate_true_inverts_sense():
    # Match would normally succeed (``passed == False`` matches ``const:
    # False``), so the predicate would fire without negate. With ``negate:
    # true`` the predicate should NOT fire because the match did not fail.
    edge = {
        "condition": {
            "pointer": "/verdict/passed",
            "match": {"const": False},
            "negate": True,
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, JSON)
    assert outcome.fired is False
    assert "match succeeded" in outcome.reason


# ── Edge cases (F2 / F3) ──────────────────────────────────────────────────────


def test_bad_pointer_missing_key_warns_and_does_not_fire():
    # ``/verdict/missing`` is not in the artifact. Per F3: warn, do not fire
    # (because ``negate`` is False).
    edge = {
        "condition": {
            "pointer": "/verdict/missing",
            "match": {"const": "anything"},
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, JSON)
    assert outcome.fired is False
    assert "does not address" in outcome.warning


def test_negate_true_with_missing_pointer_fires_as_match_failure():
    # Per F3: with ``negate: true`` the missing key counts as match failure.
    edge = {
        "condition": {
            "pointer": "/verdict/missing",
            "match": {"const": "anything"},
            "negate": True,
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, JSON)
    assert outcome.fired is True
    assert "negated" in outcome.reason
    assert "does not address" in outcome.warning


def test_non_json_artifact_warns_and_does_not_fire():
    # Per F2: missing or non-JSON artifact → warn, do NOT fire on condition.
    # The rc channel stays authoritative.
    edge = {
        "condition": {
            "pointer": "/verdict/passed",
            "match": {"const": False},
        }
    }
    compiled = compile_condition(edge)
    outcome = evaluate_condition(compiled, "<<<not json>>>")
    assert outcome.fired is False
    assert "not valid JSON" in outcome.warning


# ── Compile-time rejection (C2) ───────────────────────────────────────────────


def test_compile_time_rejects_unknown_key_in_condition():
    edge = {
        "condition": {
            "pointer": "/x",
            "match": {"const": 1},
            "bogus": True,
        }
    }
    with pytest.raises(ConditionSchemaError) as exc_info:
        validate_condition_schema(edge)
    # The error must surface the schema path so the recipe author can find
    # the offending key in their workflow.yaml.
    assert "bogus" in str(exc_info.value)
    assert "condition.<root>" in str(exc_info.value)


def test_compile_time_rejects_non_string_pointer():
    edge = {
        "condition": {
            "pointer": 42,  # must be a string
            "match": {"const": 1},
        }
    }
    with pytest.raises(ConditionSchemaError) as exc_info:
        validate_condition_schema(edge)
    assert "pointer" in str(exc_info.value)
    assert "string" in str(exc_info.value)


def test_compile_time_rejects_non_dict_match():
    edge = {
        "condition": {
            "pointer": "/x",
            "match": "not-a-dict",  # must be a mapping (JSON Schema fragment)
        }
    }
    with pytest.raises(ConditionSchemaError) as exc_info:
        validate_condition_schema(edge)
    assert "match" in str(exc_info.value)
    assert "object" in str(exc_info.value)


def test_compile_time_rejects_non_bool_negate():
    edge = {
        "condition": {
            "pointer": "/x",
            "match": {"const": 1},
            "negate": "yes",  # must be a boolean
        }
    }
    with pytest.raises(ConditionSchemaError) as exc_info:
        validate_condition_schema(edge)
    assert "negate" in str(exc_info.value)
    assert "boolean" in str(exc_info.value)


def test_compile_time_rejects_malformed_json_schema_match():
    # ``type`` must be a string or list of strings, not an int — Draft7
    # schema validation should catch this and surface it as a
    # ``ConditionSchemaError`` (not a generic ``SchemaError``).
    edge = {
        "condition": {
            "pointer": "/x",
            "match": {"type": 7},
        }
    }
    with pytest.raises(ConditionSchemaError) as exc_info:
        validate_condition_schema(edge)
    assert "match" in str(exc_info.value)
    assert "JSON Schema" in str(exc_info.value)


def test_compile_time_rejects_empty_pointer():
    # An empty pointer resolves to the whole artifact (RFC 6901 §4) — almost
    # always a mistake. Surface the error so the recipe author does not
    # silently match against the entire document.
    edge = {
        "condition": {
            "pointer": "",
            "match": {"const": 1},
        }
    }
    with pytest.raises(ConditionSchemaError) as exc_info:
        validate_condition_schema(edge)
    assert "pointer" in str(exc_info.value)
    assert "non-empty" in str(exc_info.value)


def test_compile_time_accepts_negate_true():
    # ``negate: true`` is a valid opt-in. The compile-time validator must
    # accept a real boolean, not reject it as a type error.
    edge = {
        "condition": {
            "pointer": "/x",
            "match": {"const": 1},
            "negate": True,
        }
    }
    # No exception expected.
    validate_condition_schema(edge)
    compiled = compile_condition(edge)
    assert compiled.negate is True