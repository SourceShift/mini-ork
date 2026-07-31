"""P0: behavioral verifier — API surface (``mini_ork.verify.behavioral``).

Covers the three-valued verdict (PROVEN / REFUTED / UNVERIFIED), the honest
abstain-does-not-pass rule, metamorphic relations, the path-escape guard, and
the process-seam exit-code mapping. No network: the HTTP requester is injected.
"""
from __future__ import annotations

import json

import pytest

from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    HttpResult,
    Observable,
    ObservableError,
    _exit_code,
    main,
    observable_from_env,
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
    return HttpResult(status, body if body is not None else {"status": "ok"}, "", ok_transport=True)


def _api(**over):
    base = {"surface": "api", "staging_url": "https://staging.example", "target": "/health"}
    base.update(over)
    return Observable.from_mapping(base)


# --- verdict resolution --------------------------------------------------- #
def test_proven_on_status_and_idempotent():
    obs = _api(expect_status=[200], metamorphic=["idempotent_repeat"])
    v = run_api_check(obs, requester=FakeRequester(_ok(), _ok()))
    assert v.status == PROVEN
    assert v.to_json()  # non-empty evidence for the dispatcher


def test_refuted_on_bad_status():
    obs = _api(expect_status=[200])
    v = run_api_check(obs, requester=FakeRequester(_ok(status=500)))
    assert v.status == REFUTED
    assert any(c.name == "status" and c.ok is False for c in v.checks)


def test_refuted_on_missing_required_key():
    obs = _api(expect_json_schema={"type": "object", "required": ["status"]})
    v = run_api_check(obs, requester=FakeRequester(_ok(body={})))
    assert v.status == REFUTED
    assert any(c.name == "json_shape" and c.ok is False for c in v.checks)


def test_refuted_when_idempotent_repeat_diverges():
    obs = _api(metamorphic=["idempotent_repeat"])
    # P2 amplification runs N=3 probes; queue a 3rd result that bounces back to
    # the canonical body so the 3 amplify probes see {changed, ok, ok} — still
    # divergent, so the verdict is REFUTED.
    reqr = FakeRequester(
        _ok(body={"status": "ok"}),
        _ok(body={"status": "changed"}),
        _ok(body={"status": "ok"}),
    )
    v = run_api_check(obs, requester=reqr)
    assert v.status == REFUTED


def test_unverified_when_unreachable():
    obs = _api()
    reqr = FakeRequester(HttpResult(0, None, "", ok_transport=False, error="ConnectError"))
    v = run_api_check(obs, requester=reqr)
    assert v.status == UNVERIFIED


def test_unverified_when_nothing_to_probe():
    obs = Observable.from_mapping({"surface": "api"})  # no staging_url/target
    v = run_api_check(obs, requester=FakeRequester(_ok()))
    assert v.status == UNVERIFIED


def test_unverified_when_relation_not_evaluable():
    obs = _api(metamorphic=["filtered_subset_of_unfiltered"])
    v = run_api_check(obs, requester=FakeRequester(_ok(), _ok()))
    assert v.status == UNVERIFIED  # abstains rather than falsely passing


def test_failure_outranks_abstention():
    # A broken status AND an unevaluable relation must still REFUTE, not abstain.
    obs = _api(expect_status=[200], metamorphic=["filtered_subset_of_unfiltered"])
    v = run_api_check(obs, requester=FakeRequester(_ok(status=500), _ok(status=500)))
    assert v.status == REFUTED


def test_order_invariant_holds_across_reordered_lists():
    obs = _api(metamorphic=["order_invariant"])
    reqr = FakeRequester(_ok(body=[1, 2, 3]), _ok(body=[3, 1, 2]))
    v = run_api_check(obs, requester=reqr)
    assert v.status == PROVEN


# --- dispatch + unimplemented surfaces ------------------------------------ #
def test_ui_surface_unverified_in_p0():
    obs = Observable.from_mapping({"surface": "ui", "target": "/signup"})
    v = run(obs)
    assert v.status == UNVERIFIED


def test_run_dispatches_api_surface():
    obs = _api(expect_status=[200])
    v = run(obs, requester=FakeRequester(_ok()))
    assert v.status == PROVEN


# --- descriptor validation ------------------------------------------------ #
def test_path_escape_rejected():
    with pytest.raises(ObservableError):
        Observable.from_mapping({"surface": "api", "target": "../etc/passwd"})


def test_unknown_surface_rejected():
    with pytest.raises(ObservableError):
        Observable.from_mapping({"surface": "grpc"})


def test_unknown_metamorphic_rejected():
    with pytest.raises(ObservableError):
        Observable.from_mapping({"surface": "api", "metamorphic": ["teleport"]})


# --- env parsing ---------------------------------------------------------- #
def test_observable_from_env_none_when_undeclared():
    assert observable_from_env(env={}) is None


def test_observable_from_env_builds_from_behav_vars():
    env = {
        "MO_BEHAV_SURFACE": "api",
        "MO_BEHAV_STAGING_URL": "https://staging.example",
        "MO_BEHAV_TARGET": "/health",
        "MO_BEHAV_EXPECT_STATUS": "200,204",
        "MO_BEHAV_METAMORPHIC": "idempotent_repeat",
    }
    obs = observable_from_env(env=env)
    assert obs is not None
    assert obs.expect_status == [200, 204]
    assert obs.metamorphic == ["idempotent_repeat"]


# --- exit-code mapping (process seam) ------------------------------------- #
def test_exit_code_mapping():
    assert _exit_code(PROVEN, abstain_exit=1) == 0
    assert _exit_code(REFUTED, abstain_exit=1) == 1
    assert _exit_code(UNVERIFIED, abstain_exit=1) == 1  # conservative default
    assert _exit_code(UNVERIFIED, abstain_exit=0) == 0  # advisory-only override


def test_main_prints_json_and_returns_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("MO_BEHAV_SURFACE", "api")
    monkeypatch.setenv("MO_BEHAV_STAGING_URL", "https://staging.example")
    monkeypatch.setenv("MO_BEHAV_TARGET", "/health")
    rc = main(requester=FakeRequester(_ok()))
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == PROVEN
    assert payload["pass"] is True
    assert rc == 0


def test_main_abstains_nonzero_when_no_observable(monkeypatch, capsys):
    for k in ("MO_OBSERVABLE_SPEC", "MO_BEHAV_SURFACE"):
        monkeypatch.delenv(k, raising=False)
    rc = main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == UNVERIFIED
    assert rc == 1  # abstain must not green a run


def test_main_abstain_exit_override(monkeypatch, capsys):
    monkeypatch.delenv("MO_BEHAV_SURFACE", raising=False)
    monkeypatch.setenv("MO_BEHAV_ABSTAIN_EXIT", "0")
    rc = main()
    assert json.loads(capsys.readouterr().out)["status"] == UNVERIFIED
    assert rc == 0
