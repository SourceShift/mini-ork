"""P2 oracle hardening — JSON-Schema validation, metamorphic amplification, journeys.

Three new oracle capabilities land here, all driven through the FakeRequester
harness the P0 verifier tests use:

1. ``_validate_json_schema`` lazy-imports jsonschema (Draft 2020-12) when
   present and falls back to type+required on stdlib-only environments.
2. ``_amplify`` runs n probes (capped against ``budget.max_turns``) so a
   ``idempotent_repeat`` divergence on the *3rd* probe is caught, and adds
   ``filtered_subset_of_unfiltered`` evaluation when ``observable.filter`` is
   declared.
3. ``run_journey_check`` runs nested observables in order with ``${name}``
   substitution guarded by ``_guard_path_escape``.

P0 surface is unchanged: any P0 observable still produces the same verdict it
did before this change (additional ``steps`` / ``filter`` / ``extract`` fields
default to empty).
"""
from __future__ import annotations

import sys

import pytest

from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    HttpResult,
    Observable,
    _amplify,
    _default_requester,  # noqa: F401  (imported to verify the module stays pure)
    _extract,
    _guard_path_escape,
    _shape_ok,
    _substitute,
    _validate_json_schema,
    run_api_check,
    run_journey_check,
)


class FakeRequester:
    """Returns queued HttpResults in order; repeats the last for extra calls."""

    def __init__(self, *results: HttpResult):
        self._results = list(results)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method: str, url: str, **kwargs) -> HttpResult:
        self.calls.append((method, url, kwargs))
        idx = min(len(self.calls) - 1, len(self._results) - 1)
        return self._results[idx]


def _ok(status=200, body=None):
    return HttpResult(
        status,
        body if body is not None else {"status": "ok"},
        "",
        ok_transport=True,
    )


def _api(**over):
    base = {
        "surface": "api",
        "staging_url": "https://staging.example",
        "target": "/health",
    }
    base.update(over)
    return Observable.from_mapping(base)


# --------------------------------------------------------------------------- #
# _validate_json_schema: jsonschema path + lazy-import fallback
# --------------------------------------------------------------------------- #
def test_validate_json_schema_ok_with_real_jsonschema():
    schema = {
        "type": "object",
        "required": ["id", "name"],
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    }
    sc = _validate_json_schema({"id": 1, "name": "x"}, schema)
    assert sc.ok is True
    assert sc.reason == ""


def test_validate_json_schema_refutes_nested_missing_key():
    schema = {
        "type": "object",
        "required": ["a", "b"],
        "properties": {
            "a": {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}
        },
    }
    sc = _validate_json_schema({"a": {"x": 1}}, schema)  # wrong inner type + missing top-level b
    assert sc.ok is False
    assert "b" in sc.reason  # first failure names the missing top-level key


def test_validate_json_schema_lazy_fallback_when_jsonschema_missing(monkeypatch):
    """When jsonschema is unimportable, the fallback path uses _shape_ok.

    Simulates the verifier_contract check ``jsonschema_lazy_fallback``: block
    the import and confirm the function still returns a usable _SchemaCheck.
    """
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    # Force re-evaluation of the inner import inside _validate_json_schema
    sc = _validate_json_schema({"a": 1}, {"type": "object", "required": ["a"]})
    assert sc.ok is True
    sc_missing = _validate_json_schema({"b": 1}, {"type": "object", "required": ["a"]})
    assert sc_missing.ok is False
    assert "a" in sc_missing.reason


def test_shape_ok_fallback_preserved():
    """P0 shape check still works (called by the fallback path)."""
    ok, reason = _shape_ok({"a": 1}, {"type": "object", "required": ["a"]})
    assert ok is True
    assert reason == ""
    ok2, reason2 = _shape_ok({}, {"type": "object", "required": ["a"]})
    assert ok2 is False
    assert "a" in reason2


# --------------------------------------------------------------------------- #
# run_api_check: nested-schema PROVEN/REFUTED via _validate_json_schema
# --------------------------------------------------------------------------- #
def test_run_api_check_proven_against_nested_schema():
    obs = _api(
        expect_json_schema={
            "type": "object",
            "required": ["id", "meta"],
            "properties": {
                "id": {"type": "integer"},
                "meta": {"type": "object", "required": ["version"]},
            },
        }
    )
    v = run_api_check(obs, requester=FakeRequester(_ok(body={"id": 1, "meta": {"version": "v1"}})))
    assert v.status == PROVEN


def test_run_api_check_refuted_when_nested_schema_violated():
    obs = _api(
        expect_json_schema={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer"}},
        }
    )
    v = run_api_check(obs, requester=FakeRequester(_ok(body={"id": "not-an-int"})))
    assert v.status == REFUTED
    assert any(c.name == "json_shape" and c.ok is False for c in v.checks)


# --------------------------------------------------------------------------- #
# _amplify: idempotent_repeat REFUTES on 3rd divergence, order_invariant,
# filtered_subset_of_unfiltered with declared filter, budget cap.
# --------------------------------------------------------------------------- #
def test_amplify_idempotent_repeat_refutes_on_third_divergence():
    obs = _api()  # default budget max_turns=12, no cap firing here
    reqr = FakeRequester(
        _ok(body={"v": 1}),
        _ok(body={"v": 1}),     # probes 1+2 match
        _ok(body={"v": 2}),     # probe 3 diverges — must REFUTE
    )
    c = _amplify("idempotent_repeat", "GET", "https://staging.example/health", reqr, obs, n=3)
    assert c.ok is False
    assert "3 probes diverged" in c.detail


def test_amplify_idempotent_repeat_proven_when_all_identical():
    obs = _api()
    reqr = FakeRequester(_ok(body={"v": 1}), _ok(body={"v": 1}), _ok(body={"v": 1}))
    c = _amplify("idempotent_repeat", "GET", "https://x/h", reqr, obs, n=3)
    assert c.ok is True
    assert "3 probes identical" in c.detail


def test_amplify_caps_n_against_budget_max_turns():
    """Budget.max_turns=2 forces the run down to 2 probes; cap is reported."""
    obs = _api(budget={"max_tokens": 1, "max_turns": 2})
    # 3 different bodies queued — with cap=2 we only see 2, so set-equal = diverged
    reqr = FakeRequester(_ok(body={"v": 1}), _ok(body={"v": 2}))
    c = _amplify("idempotent_repeat", "GET", "https://x/h", reqr, obs, n=10)
    assert c.ok is False
    assert "capped" in c.detail
    assert "2 probes diverged" in c.detail


def test_amplify_order_invariant_proven_across_reorders():
    obs = _api()
    reqr = FakeRequester(_ok(body=[1, 2, 3]), _ok(body=[3, 1, 2]))
    c = _amplify("order_invariant", "GET", "https://x/list", reqr, obs)
    assert c.ok is True


def test_amplify_filtered_subset_proven():
    obs = _api(filter="active", metamorphic=["filtered_subset_of_unfiltered"])
    # unfiltered returns 3 items; filtered returns the 2 "active" ones
    reqr = FakeRequester(
        _ok(body=[{"id": 1}, {"id": 2}, {"id": 3}]),
        _ok(body=[{"id": 1}, {"id": 2}]),
    )
    c = _amplify(
        "filtered_subset_of_unfiltered",
        "GET",
        "https://x/items",
        reqr,
        obs,
    )
    assert c.ok is True
    assert "filtered ⊆ unfiltered" in c.detail


def test_amplify_filtered_subset_refuted_when_not_subset():
    obs = _api(filter="active", metamorphic=["filtered_subset_of_unfiltered"])
    # filtered returns id=99, which is not in the unfiltered set
    reqr = FakeRequester(
        _ok(body=[{"id": 1}, {"id": 2}]),
        _ok(body=[{"id": 99}]),
    )
    c = _amplify(
        "filtered_subset_of_unfiltered",
        "GET",
        "https://x/items",
        reqr,
        obs,
    )
    assert c.ok is False
    assert "not a subset" in c.detail


def test_amplify_filtered_subset_abstains_without_filter():
    obs = _api()  # no filter declared
    reqr = FakeRequester(_ok(body=[1, 2, 3]), _ok(body=[1, 2]))
    c = _amplify(
        "filtered_subset_of_unfiltered",
        "GET",
        "https://x/items",
        reqr,
        obs,
    )
    assert c.ok is None
    assert "observable.filter" in c.detail


# --------------------------------------------------------------------------- #
# Journey: ${var} substitution + 2-step PROVEN/REFUTE-on-step-2
# --------------------------------------------------------------------------- #
def test_substitute_basic_and_unbound_left_intact():
    assert _substitute("/users/${uid}", {"uid": 7}) == "/users/7"
    assert _substitute("/users/${uid}", {}) == "/users/${uid}"
    assert _substitute("", {"uid": 7}) == ""


def test_extract_walks_dotted_path():
    assert _extract({"a": {"b": {"c": 42}}}, "a.b.c") == 42
    assert _extract({"a": [{"b": 7}]}, "a.0.b") == 7
    assert _extract({"a": 1}, "a.b") is None  # walks into non-container
    assert _extract(None, "a") is None
    assert _extract({"a": 1}, "") is None


def test_guard_path_escape_rejects_resolved_dotdot_in_journey():
    """A malicious extract that resolves to '..' must be caught by the guard."""
    # Simulate a buggy/malicious extract binding that yields a path-escape.
    bad_target = "/users/" + _substitute("${uid}", {"uid": "../../etc/passwd"})
    with pytest.raises(Exception):
        _guard_path_escape(bad_target)


def test_journey_two_step_proven_with_var_extraction():
    obs = Observable.from_mapping(
        {
            "surface": "journey",
            "steps": [
                {
                    "surface": "api",
                    "staging_url": "https://staging.example",
                    "target": "/users",
                    "method": "POST",
                    "expect_status": [200, 201],
                    "extract": {"name": "uid", "path": "id"},
                },
                {
                    "surface": "api",
                    "target": "/users/${uid}",
                    "method": "GET",
                    "expect_status": [200],
                },
            ],
        }
    )
    # POST /users returns {"id": 7}; GET /users/7 returns ok
    reqr = FakeRequester(
        _ok(status=201, body={"id": 7}),
        _ok(body={"id": 7, "name": "alice"}),
        _ok(body={"id": 7, "name": "alice"}),
    )
    v = run_journey_check(obs, requester=reqr)
    assert v.status == PROVEN, v.evidence
    assert any(c.name == "step[1]:api" and c.ok is True for c in v.checks)


def test_journey_refutes_when_step_two_breaks():
    obs = Observable.from_mapping(
        {
            "surface": "journey",
            "steps": [
                {
                    "surface": "api",
                    "staging_url": "https://staging.example",
                    "target": "/users",
                    "method": "POST",
                    "expect_status": [200, 201],
                    "extract": {"name": "uid", "path": "id"},
                },
                {
                    "surface": "api",
                    "target": "/users/${uid}",
                    "method": "GET",
                    "expect_status": [200],
                },
            ],
        }
    )
    # POST succeeds (returns id=7); GET /users/7 returns 404
    reqr = FakeRequester(
        _ok(status=201, body={"id": 7}),
        _ok(status=404, body={"error": "not found"}),
        _ok(status=404, body={"error": "not found"}),
    )
    v = run_journey_check(obs, requester=reqr)
    assert v.status == REFUTED
    assert any(c.name == "step[1]:api" and c.ok is False for c in v.checks)


def test_journey_short_circuits_on_first_refute():
    """Step 1 REFUTED → step 2 never gets probed."""
    obs = Observable.from_mapping(
        {
            "surface": "journey",
            "steps": [
                {
                    "surface": "api",
                    "staging_url": "https://staging.example",
                    "target": "/users",
                    "method": "POST",
                    "expect_status": [200, 201],
                },
                {
                    "surface": "api",
                    "target": "/users/999",
                    "method": "GET",
                    "expect_status": [200],
                },
            ],
        }
    )
    reqr = FakeRequester(_ok(status=500))  # step 1 fails
    v = run_journey_check(obs, requester=reqr)
    assert v.status == REFUTED
    # Only the first request should have been issued for the step (no GET yet)
    assert len(reqr.calls) == 1


def test_journey_unverified_when_no_steps():
    obs = Observable.from_mapping({"surface": "journey"})
    v = run_journey_check(obs, requester=FakeRequester())
    assert v.status == UNVERIFIED