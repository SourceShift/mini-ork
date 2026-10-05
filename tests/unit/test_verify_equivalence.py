"""Equivalence operators for the behavioral verifier (``mini_ork.verify.equivalence``).

Hermetic: the HTTP requester is injected (no network), no LLM is called, and the
module under test is pure stdlib. Covers the four built-in operators, the
declaration plumbing in ``behavioral.py``, the honest-abstain guard for unknown /
invalid operators, the process entrypoint, and the OCP registry seam.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.verify import equivalence as eq
from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    HttpResult,
    Observable,
    ObservableError,
    main,
    run,
    run_api_check,
)


class FakeRequester:
    """Returns queued HttpResults in order; repeats the last for extra calls."""

    def __init__(self, *results: HttpResult):
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, **_kw) -> HttpResult:
        self.calls.append((method, url))
        idx = min(len(self.calls) - 1, len(self._results) - 1)
        return self._results[idx]


def _ok(status=200, body=None):
    return HttpResult(status, body, "", ok_transport=True)


@pytest.fixture(autouse=True)
def _restore_operators():
    """Snapshot/restore the operator registry around every test (test 11 mutates it)."""
    snapshot = dict(eq._OPERATORS)
    yield
    eq._OPERATORS.clear()
    eq._OPERATORS.update(snapshot)


def _four_specs():
    """exact, set, canonical(ignore ts + strip + numeric), tolerant(abs_tol=0.01)."""
    return [
        eq.EquivalenceSpec.from_raw("exact"),
        eq.EquivalenceSpec.from_raw("set"),
        eq.EquivalenceSpec(
            operator="canonical",
            rules={"ignore_keys": ["ts"], "strip_whitespace": True, "numeric": True},
        ),
        eq.EquivalenceSpec(operator="tolerant", rules={"abs_tol": 0.01}),
    ]


# --- 1. set ----------------------------------------------------------------- #
def test_set_relaxes_list_order():
    assert eq.compare(["b", "a"], ["a", "b"]).equal is False  # exact
    r = eq.compare(["b", "a"], ["a", "b"], eq.EquivalenceSpec.from_raw("set"))
    assert r.equal is True and r.operator == "set"

    obs = Observable.from_mapping(
        {
            "surface": "api",
            "staging_url": "https://staging.example",
            "target": "/x",
            "expect_body": ["a", "b"],
            "equivalence": "set",
        }
    )
    v = run_api_check(obs, requester=FakeRequester(_ok(body=["b", "a"])))
    assert v.status == PROVEN

    obs_exact = Observable.from_mapping(
        {
            "surface": "api",
            "staging_url": "https://staging.example",
            "target": "/x",
            "expect_body": ["a", "b"],
        }
    )
    v = run_api_check(obs_exact, requester=FakeRequester(_ok(body=["b", "a"])))
    assert v.status == REFUTED


# --- 2. canonical ----------------------------------------------------------- #
def test_canonical_rules():
    observed = {"ok": True, "msg": "all  good\n", "n": 1.0, "ts": "..."}
    expected = {"n": 1, "msg": "all good", "ok": True}
    rules = {"ignore_keys": ["ts"], "strip_whitespace": True, "numeric": True}
    spec = eq.EquivalenceSpec(operator="canonical", rules=rules)

    assert eq.compare(observed, expected).equal is False  # exact
    assert eq.compare(observed, expected, spec).equal is True

    # key-order-only difference is already equal under exact
    assert eq.compare({"a": 1, "b": 2}, {"b": 2, "a": 1}).equal is True

    # changing `ok` to false → False with path "$.ok"
    bad = {"ok": False, "msg": "all  good\n", "n": 1.0, "ts": "..."}
    r = eq.compare(bad, expected, spec)
    assert r.equal is False
    assert r.path == "$.ok"


# --- 3. tolerant ------------------------------------------------------------ #
def test_tolerant():
    abs_spec = eq.EquivalenceSpec(operator="tolerant", rules={"abs_tol": 0.01})
    assert eq.compare(1.004, 1.0, abs_spec).equal is True
    assert eq.compare(1.02, 1.0, abs_spec).equal is False

    rel_spec = eq.EquivalenceSpec(operator="tolerant", rules={"rel_tol": 0.1})
    assert eq.compare(105, 100, rel_spec).equal is True
    assert eq.compare(120, 100, rel_spec).equal is False

    # bool vs bool is never compared as a number
    bool_spec = eq.EquivalenceSpec(operator="tolerant", rules={"abs_tol": 1.0})
    assert eq.compare(True, False, bool_spec).equal is False

    # no tolerance declared → spec_error non-empty and equal is None
    empty = eq.EquivalenceSpec(operator="tolerant", rules={})
    assert eq.spec_error(empty) != ""
    assert eq.compare(1.0, 1.0, empty).equal is None


# --- 4. unknown / invalid operator never passes ----------------------------- #
def test_unknown_operator_never_passes():
    fuzzy = eq.EquivalenceSpec.from_raw("fuzzy")
    assert eq.spec_error(fuzzy) != ""
    assert eq.compare({"a": 1}, {"a": 1}, fuzzy).equal is None

    typo = eq.EquivalenceSpec(operator="canonical", rules={"ignore_key": ["ts"]})
    assert eq.spec_error(typo) != ""
    assert eq.compare({"a": 1}, {"a": 1}, typo).equal is None

    obs = Observable.from_mapping(
        {
            "surface": "api",
            "staging_url": "https://staging.example",
            "target": "/x",
            "expect_body": {"a": 1},
            "equivalence": "fuzzy",
        }
    )
    v = run_api_check(obs, requester=FakeRequester(_ok(body={"a": 1})))
    assert v.status == UNVERIFIED  # body matches, but the operator is unknown

    obs_ui = Observable.from_mapping(
        {"surface": "ui", "target": "/x", "equivalence": "fuzzy"}
    )
    assert run(obs_ui).status == UNVERIFIED


# --- 5. default is byte-identical ------------------------------------------- #
def test_default_is_byte_identical():
    obs = Observable.from_mapping(
        {
            "surface": "api",
            "staging_url": "https://staging.example",
            "target": "/health",
            "metamorphic": ["idempotent_repeat"],
        }
    )
    v = run_api_check(
        obs, requester=FakeRequester(_ok(body={"status": "ok"}))
    )
    payload = json.loads(v.to_json())
    assert payload.pop("operator") == "exact"
    for check in payload["checks"]:
        check.pop("operator", None)
    assert payload == {
        "verifier": "behavioral",
        "surface": "api",
        "target": "https://staging.example/health",
        "status": "PROVEN",
        "pass": True,
        "checks": [
            {"name": "status", "ok": True, "detail": "got 200, expected one of [200]"},
            {"name": "idempotent_repeat", "ok": True, "detail": "3 probes identical"},
        ],
        "evidence": (
            "PROVEN: https://staging.example/health\n"
            "  [PASS] status: got 200, expected one of [200]\n"
            "  [PASS] idempotent_repeat: 3 probes identical"
        ),
    }

    # (a, a, b) with repeat-last → REFUTED, names/ok unchanged
    a = {"uptime": 1.0}
    b = {"uptime": 2.0}
    v2 = run_api_check(
        obs,
        requester=FakeRequester(
            _ok(body=a), _ok(body=a), _ok(body=b)
        ),
    )
    payload2 = json.loads(v2.to_json())
    assert payload2["status"] == REFUTED
    assert [c["name"] for c in payload2["checks"]] == ["status", "idempotent_repeat"]
    assert [c["ok"] for c in payload2["checks"]] == [True, False]
    idem = payload2["checks"][1]
    assert idem["detail"].startswith("3 probes diverged across 2 distinct bodies")
    assert "$.uptime" in idem["detail"]
    assert idem["detail"].endswith("[operator=exact]")


# --- 6. mutant set: genuinely different values are False under every operator #
def test_mutant_set_all_false():
    pairs = [
        (1.05, 1.0),                        # scalar, delta > 0.01
        ("hello", "world"),                 # string
        (True, False),                      # bool flip
        ([1, 2, 3], [1, 2]),                # dropped list element
        ({"a": 1, "b": 2}, {"a": 1}),       # extra key
        ("1", 1),                           # string "1" vs int 1
        ([1, 1, 2], [1, 2, 2]),             # multiset differs
    ]
    for observed, expected in pairs:
        for spec in _four_specs():
            r = eq.compare(observed, expected, spec)
            assert r.equal is False, (
                f"{spec.operator} should REFUTE {observed!r} vs {expected!r}"
            )


# --- 7. flip measurement over 4 fitted fixtures ----------------------------- #
def test_flip_measurement():
    fixtures = [
        # (observed, expected, fitted operator)
        (["b", "a"], ["a", "b"], eq.EquivalenceSpec.from_raw("set")),
        (
            {"ts": "x", "v": 1},
            {"v": 1},
            eq.EquivalenceSpec(operator="canonical", rules={"ignore_keys": ["ts"]}),
        ),
        (
            1.004,
            1.0,
            eq.EquivalenceSpec(operator="tolerant", rules={"abs_tol": 0.01}),
        ),
        (
            "all  good\n",
            "all good",
            eq.EquivalenceSpec(operator="canonical", rules={"strip_whitespace": True}),
        ),
    ]
    flips = 0
    for observed, expected, fitted in fixtures:
        assert eq.compare(observed, expected).equal is False  # exact refutes
        assert eq.compare(observed, expected, fitted).equal is True  # fitted proves
        flips += 1
    assert flips == 4

    # false_approvals == 0 over the mutant set (test 6)
    mutant_pairs = [
        (1.05, 1.0),
        ("hello", "world"),
        (True, False),
        ([1, 2, 3], [1, 2]),
        ({"a": 1, "b": 2}, {"a": 1}),
        ("1", 1),
        ([1, 1, 2], [1, 2, 2]),
    ]
    false_approvals = 0
    for observed, expected in mutant_pairs:
        for spec in _four_specs():
            if eq.compare(observed, expected, spec).equal is True:
                false_approvals += 1
    assert false_approvals == 0


# --- 8. idempotent_repeat over volatile uptime ------------------------------ #
def test_idempotent_uptime():
    def reqr(*bodies):
        return FakeRequester(
            *[_ok(body=b) for b in bodies]
        )

    exact_obs = Observable.from_mapping(
        {
            "surface": "api",
            "staging_url": "https://staging.example",
            "target": "/x",
            "metamorphic": ["idempotent_repeat"],
        }
    )
    v = run_api_check(
        exact_obs,
        requester=reqr(
            {"status": "ok", "uptime": 1.001},
            {"status": "ok", "uptime": 1.002},
            {"status": "ok", "uptime": 1.003},
        ),
    )
    assert v.status == REFUTED

    canon_obs = Observable.from_mapping(
        {
            "surface": "api",
            "staging_url": "https://staging.example",
            "target": "/x",
            "metamorphic": ["idempotent_repeat"],
            "equivalence": {"operator": "canonical", "rules": {"ignore_keys": ["uptime"]}},
        }
    )
    v = run_api_check(
        canon_obs,
        requester=reqr(
            {"status": "ok", "uptime": 1.001},
            {"status": "ok", "uptime": 1.002},
            {"status": "ok", "uptime": 1.003},
        ),
    )
    assert v.status == PROVEN

    tol_obs = Observable.from_mapping(
        {
            "surface": "api",
            "staging_url": "https://staging.example",
            "target": "/x",
            "metamorphic": ["idempotent_repeat"],
            "equivalence": {"operator": "tolerant", "rules": {"abs_tol": 5}},
        }
    )
    v = run_api_check(
        tol_obs,
        requester=reqr(
            {"status": "ok", "uptime": 1.001},
            {"status": "ok", "uptime": 1.002},
            {"status": "ok", "uptime": 1.003},
        ),
    )
    assert v.status == PROVEN

    # a probe whose status flips to "down" stays REFUTED under both
    down_bodies = [
        {"status": "ok", "uptime": 1.001},
        {"status": "ok", "uptime": 1.002},
        {"status": "down", "uptime": 1.003},
    ]
    assert run_api_check(canon_obs, requester=reqr(*down_bodies)).status == REFUTED
    assert run_api_check(tol_obs, requester=reqr(*down_bodies)).status == REFUTED


# --- 9. REFUTED verdicts carry the declared operator ------------------------ #
def test_refuted_carries_operator_payload():
    cases = [
        (
            Observable.from_mapping(
                {
                    "surface": "api",
                    "staging_url": "https://staging.example",
                    "target": "/x",
                    "expect_body": ["a", "b"],
                    "equivalence": "exact",
                }
            ),
            FakeRequester(_ok(body=["b", "a"])),
            "exact",
            "expect_body",
        ),
        (
            Observable.from_mapping(
                {
                    "surface": "api",
                    "staging_url": "https://staging.example",
                    "target": "/x",
                    "metamorphic": ["idempotent_repeat"],
                }
            ),
            FakeRequester(
                _ok(body={"uptime": 1.0}),
                _ok(body={"uptime": 1.0}),
                _ok(body={"uptime": 2.0}),
            ),
            "exact",
            "idempotent_repeat",
        ),
    ]
    for obs, reqr, op, failing_name in cases:
        v = run_api_check(obs, requester=reqr)
        assert v.status == REFUTED
        payload = json.loads(v.to_json())
        assert payload["operator"] == op
        failing = [
            c for c in payload["checks"]
            if c["name"] == failing_name and c["ok"] is False
        ]
        assert failing, f"no failing {failing_name} check"
        for check in failing:
            assert check["operator"] == op
            assert check["detail"].endswith(f"[operator={op}]")


# --- 10. production entrypoint (behavioral.main via env / spec file) -------- #
def test_production_entrypoint(tmp_path, monkeypatch, capsys):
    def body_requester():
        return FakeRequester(_ok(body=["b", "a"]))

    def write_spec(text: str) -> str:
        path = tmp_path / "obs.yaml"
        path.write_text(text)
        return str(path)

    # set → rc 0, PROVEN, operator "set"
    monkeypatch.setenv(
        "MO_OBSERVABLE_SPEC",
        write_spec(
            "surface: api\n"
            "staging_url: https://staging.example\n"
            "target: /x\n"
            "expect_body: [a, b]\n"
            "equivalence: set\n"
        ),
    )
    rc = main([], requester=body_requester())
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == PROVEN
    assert payload["operator"] == "set"

    # exact → rc 1, REFUTED
    monkeypatch.setenv(
        "MO_OBSERVABLE_SPEC",
        write_spec(
            "surface: api\n"
            "staging_url: https://staging.example\n"
            "target: /x\n"
            "expect_body: [a, b]\n"
            "equivalence: exact\n"
        ),
    )
    rc = main([], requester=body_requester())
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["status"] == REFUTED

    # fuzzy → rc 1, UNVERIFIED
    monkeypatch.setenv(
        "MO_OBSERVABLE_SPEC",
        write_spec(
            "surface: api\n"
            "staging_url: https://staging.example\n"
            "target: /x\n"
            "expect_body: [a, b]\n"
            "equivalence: fuzzy\n"
        ),
    )
    rc = main([], requester=body_requester())
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["status"] == UNVERIFIED

    # MO_BEHAV_* env path: canonical over volatile uptime → PROVEN
    monkeypatch.delenv("MO_OBSERVABLE_SPEC", raising=False)
    monkeypatch.setenv("MO_BEHAV_SURFACE", "api")
    monkeypatch.setenv("MO_BEHAV_STAGING_URL", "https://staging.example")
    monkeypatch.setenv("MO_BEHAV_TARGET", "/x")
    monkeypatch.setenv("MO_BEHAV_METAMORPHIC", "idempotent_repeat")
    monkeypatch.setenv(
        "MO_BEHAV_EQUIVALENCE",
        '{"operator":"canonical","rules":{"ignore_keys":["uptime"]}}',
    )
    up_req = FakeRequester(
        _ok(body={"status": "ok", "uptime": 1.001}),
        _ok(body={"status": "ok", "uptime": 1.002}),
        _ok(body={"status": "ok", "uptime": 1.003}),
    )
    rc = main([], requester=up_req)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == PROVEN


# --- 11. OCP registry seam + safety + journey + schema ---------------------- #
def test_ocp_register_and_use():
    def casefold(observed, expected, rules):
        obs = observed.lower() if isinstance(observed, str) else observed
        exp = expected.lower() if isinstance(expected, str) else expected
        if obs == exp:
            return eq.EquivalenceResult(True, "casefold")
        return eq.EquivalenceResult(
            False, "casefold",
            f"first diff at $: observed {observed!r} != expected {expected!r}",
            "$",
        )

    eq.register_operator("casefold", casefold)
    r = eq.compare("Hello", "hello", eq.EquivalenceSpec.from_raw("casefold"))
    assert r.equal is True
    assert r.operator == "casefold"


def test_raising_operator_gives_unverified():
    def boom(observed, expected, rules):
        raise RuntimeError("kaboom")

    eq.register_operator("boom", boom)
    assert eq.compare(1, 1, eq.EquivalenceSpec.from_raw("boom")).equal is None

    obs = Observable.from_mapping(
        {
            "surface": "api",
            "staging_url": "https://staging.example",
            "target": "/x",
            "expect_body": 1,
            "equivalence": "boom",
        }
    )
    v = run_api_check(obs, requester=FakeRequester(_ok(body=1)))
    assert v.status == UNVERIFIED


def test_register_bad_name_raises():
    with pytest.raises(ValueError):
        eq.register_operator("Bad Name", lambda o, e, r: eq.EquivalenceResult(True, "x"))


def test_equivalence_parse_errors():
    with pytest.raises(ObservableError):
        Observable.from_mapping({"surface": "api", "equivalence": {"rules": {}}})
    with pytest.raises(ObservableError):
        Observable.from_mapping(
            {"surface": "api", "equivalence": {"operator": "exact", "extra": 1}}
        )
    with pytest.raises(ObservableError):
        Observable.from_mapping({"surface": "api", "expect_body": {"a": object()}})


def test_journey_step_declares_set():
    obs = Observable.from_mapping(
        {
            "surface": "journey",
            "target": "/x",
            "steps": [
                {
                    "surface": "api",
                    "staging_url": "https://staging.example",
                    "target": "/step",
                    "expect_body": ["a", "b"],
                    "equivalence": "set",
                }
            ],
        }
    )
    v = run(obs, requester=FakeRequester(_ok(body=["b", "a"])))
    assert v.status == PROVEN


def test_schema_has_new_properties():
    from jsonschema import Draft202012Validator

    schema_path = (
        Path(__file__).resolve().parents[2] / "schemas" / "verifier_contract.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    props = schema["$defs"]["observable"]["properties"]
    assert "expect_body" in props
    assert "equivalence" in props

    Draft202012Validator(schema).validate(
        {
            "name": "eq",
            "kind": "behavioral",
            "observable": {
                "surface": "api",
                "expect_body": {"a": 1},
                "equivalence": {"operator": "canonical", "rules": {"ignore_keys": ["ts"]}},
            },
        }
    )
