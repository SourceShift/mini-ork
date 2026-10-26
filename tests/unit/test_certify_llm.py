"""certify's lane dispatch is bounded, retried once, and threads its caps into the dispatcher."""
from __future__ import annotations

from mini_ork.certify import llm

KNOBS = ("MO_CERTIFY_MODEL", "MO_CERTIFY_LANE_TIMEOUT_S", "MO_CERTIFY_LANE_MAX_TURNS",
         "MO_CERTIFY_LANE_RETRIES", "MO_CERTIFY_PROGRESS")


def _fake(captured, rc=0, text="def test_x():\n    assert True\n"):
    def fake(model, prompt, out_file, **kw):
        captured.append({"model": model, **kw})
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(text)
        with open(out_file + ".cost", "w", encoding="utf-8") as f:
            f.write("0.010000")
        return rc
    return fake


def _clean(monkeypatch):
    for k in KNOBS:
        monkeypatch.delenv(k, raising=False)
    llm.reset_spend()


def test_default_dispatch_bounds_every_call(monkeypatch):
    _clean(monkeypatch)
    captured: list[dict] = []
    monkeypatch.setattr(llm, "mo_llm_dispatch", _fake(captured))
    out = llm.default_dispatch("write a probe")
    assert out.startswith("def test_x")
    assert captured == [{"model": llm.DEFAULT_MODEL, "timeout_s": llm.DEFAULT_TIMEOUT_S,
                         "max_turns": llm.DEFAULT_MAX_TURNS}]
    assert llm.DEFAULT_TIMEOUT_S < 1500 and llm.DEFAULT_MAX_TURNS < 60  # tighter than the dispatcher
    assert llm.spent() == {"usd": 0.01, "calls": 1}


def test_default_dispatch_env_overrides(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("MO_CERTIFY_MODEL", "glm")
    monkeypatch.setenv("MO_CERTIFY_LANE_TIMEOUT_S", "45")
    monkeypatch.setenv("MO_CERTIFY_LANE_MAX_TURNS", "3")
    captured: list[dict] = []
    monkeypatch.setattr(llm, "mo_llm_dispatch", _fake(captured))
    assert llm.default_dispatch("p")
    assert captured == [{"model": "glm", "timeout_s": 45, "max_turns": 3}]


def test_default_dispatch_retries_once_then_abstains(monkeypatch):
    _clean(monkeypatch)
    captured: list[dict] = []
    monkeypatch.setattr(llm, "mo_llm_dispatch", _fake(captured, rc=124))
    assert llm.default_dispatch("p") == ""
    assert len(captured) == 1 + llm.DEFAULT_RETRIES
    assert llm.spent()["calls"] == 1 + llm.DEFAULT_RETRIES  # every attempt is real spend
    captured.clear()
    monkeypatch.setenv("MO_CERTIFY_LANE_RETRIES", "0")
    assert llm.default_dispatch("p") == ""
    assert len(captured) == 1


def test_default_dispatch_recovers_on_second_attempt(monkeypatch):
    _clean(monkeypatch)
    calls = {"n": 0}

    def flaky(model, prompt, out_file, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 124
        with open(out_file, "w", encoding="utf-8") as f:
            f.write("def test_mr_1():\n    assert True\n")
        return 0
    monkeypatch.setattr(llm, "mo_llm_dispatch", flaky)
    assert llm.default_dispatch("p").startswith("def test_mr_1")
    assert calls["n"] == 2


def test_default_dispatch_malformed_knob_fails_closed(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("MO_CERTIFY_LANE_TIMEOUT_S", "soon")
    captured: list[dict] = []
    monkeypatch.setattr(llm, "mo_llm_dispatch", _fake(captured))
    assert llm.default_dispatch("p") == ""
    assert captured == []  # no dispatch on a knob we could not read
    monkeypatch.setenv("MO_CERTIFY_LANE_TIMEOUT_S", "0")
    assert llm.default_dispatch("p") == "" and captured == []


def test_default_dispatch_swallows_dispatcher_crash(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("MO_CERTIFY_LANE_RETRIES", "0")

    def boom(*a, **kw):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(llm, "mo_llm_dispatch", boom)
    assert llm.default_dispatch("p") == ""


def test_mo_llm_dispatch_threads_timeout_and_max_turns(monkeypatch, tmp_path):
    from mini_ork.dispatch import llm_dispatch as ld
    from mini_ork.dispatch import providers as prov
    from mini_ork.dispatch.models import DispatchResult
    seen: dict = {}

    def fake_dispatch_model(req, *a, **kw):
        seen["req"] = req
        return DispatchResult(ok=True, rc=0, text="ok")
    monkeypatch.setattr(prov, "dispatch_model", fake_dispatch_model)
    out = tmp_path / "o.txt"
    rc = ld.mo_llm_dispatch("minimax", "hi", str(out), timeout_s=12, max_turns=3)
    assert rc == 0
    assert seen["req"].timeout_s == 12.0
    assert seen["req"].max_turns == 3  # was accepted by the signature and dropped
