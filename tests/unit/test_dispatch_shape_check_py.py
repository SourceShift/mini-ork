"""Boundary shape-check for dispatch results (SE-3 Phase A.2).

The SE-3 Layer-2 failure: a planner lane returned structurally-wrong output
(prose where the node needs JSON), no fallback was attempted, the broken text
was written to an out_file, and the run died ~20 min later when the verifier
finally tried to parse it. These tests pin the fix — a structural predicate at
the harness boundary that (a) triggers lane fallback on wrong shape and
(b) downgrades a clean-but-wrong-shape result to a shape-reject rc *there*.

The predicate is STRUCTURAL only ("is it even JSON"); semantic validation
(right keys / schema) stays validate_artifact's job. The two must not overlap.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.dispatch import llm_dispatch as L  # noqa: E402
from mini_ork.dispatch import providers as P  # noqa: E402
from mini_ork.dispatch.models import DispatchRequest, DispatchResult  # noqa: E402
from mini_ork.dispatch.predicates import (  # noqa: E402
    SHAPE_REJECT_RC,
    looks_like_json,
)
from mini_ork.dispatch.providers import dispatch_with_fallback  # noqa: E402


def _mk(text: str, *, ok: bool = True, rc: int = 0, model: str = "m") -> DispatchResult:
    return DispatchResult(ok=ok, rc=rc, text=text, model=model)


# ── predicate unit — structural plausibility only ──

@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',                              # bare object
        "[1, 2, 3]",                             # bare array
        '```json\n{"a": 1}\n```',                # fenced
        'Here is the plan: {"steps": []}. Done.',  # prose-wrapped
    ],
)
def test_looks_like_json_accepts_plausible_json(text):
    assert looks_like_json(_mk(text)) is True


@pytest.mark.parametrize(
    "text",
    [
        "I could not complete that request.",  # prose refusal
        '{"a": 1, "b":',                        # truncated
        "",                                     # empty
        "   ",                                  # whitespace only
    ],
)
def test_looks_like_json_rejects_non_json(text):
    assert looks_like_json(_mk(text)) is False


# ── dispatch_with_fallback + accept predicate ──

def test_wrong_shape_first_lane_falls_back_to_good_shape(monkeypatch):
    def fake_dispatch_model(req, root=None):
        if req.model == "lane_a":
            return _mk("sorry, no JSON here", model="lane_a")
        return _mk('{"ok": true}', model="lane_b")

    monkeypatch.setattr(P, "dispatch_model", fake_dispatch_model)
    req = DispatchRequest(model="lane_a", prompt="x")
    r = dispatch_with_fallback(req, ["lane_a", "lane_b"], accept=looks_like_json)
    assert r.ok
    assert r.model == "lane_b"
    assert r.text == '{"ok": true}'


def test_all_lanes_wrong_shape_returns_shape_reject(monkeypatch):
    monkeypatch.setattr(P, "dispatch_model", lambda req, root=None: _mk("prose", model=req.model))
    req = DispatchRequest(model="lane_a", prompt="x")
    r = dispatch_with_fallback(req, ["lane_a", "lane_b"], accept=looks_like_json)
    assert not r.ok
    assert r.rc == SHAPE_REJECT_RC
    assert "shape check" in r.error


def test_accept_none_is_backward_compatible(monkeypatch):
    # Pre-A.2 behaviour: without a predicate, prose is served as-is.
    monkeypatch.setattr(P, "dispatch_model", lambda req, root=None: _mk("prose", model=req.model))
    req = DispatchRequest(model="lane_a", prompt="x")
    r = dispatch_with_fallback(req, ["lane_a", "lane_b"], accept=None)
    assert r.ok
    assert r.text == "prose"


# ── mo_llm_dispatch single-lane path (nobody to fall back to) ──

def test_single_lane_wrong_shape_is_downgraded(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "dispatch_model", lambda req, root=None: _mk("not json", model="sonnet"))
    out = str(tmp_path / "out.txt")
    rc = L.mo_llm_dispatch("sonnet", "prompt", out, accept=looks_like_json)
    assert rc == SHAPE_REJECT_RC
    # raw text is still captured for debugging even though it is rejected …
    assert Path(out).read_text() == "not json"
    # … and the failure is surfaced on the error sidecar.
    assert Path(out + ".err.log").exists()


def test_single_lane_good_shape_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "dispatch_model", lambda req, root=None: _mk('{"k": 1}', model="sonnet"))
    out = str(tmp_path / "out.txt")
    rc = L.mo_llm_dispatch("sonnet", "prompt", out, accept=looks_like_json)
    assert rc == 0
    assert Path(out).read_text() == '{"k": 1}'


def test_single_lane_accept_none_serves_prose(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "dispatch_model", lambda req, root=None: _mk("prose", model="sonnet"))
    out = str(tmp_path / "out.txt")
    rc = L.mo_llm_dispatch("sonnet", "prompt", out, accept=None)
    assert rc == 0


# ── CLI threading contract ──

def _cli_argv(node_type: str, out: str) -> list[str]:
    return ["--node-type", node_type, "--prompt-text", "x", "--out", out, "--model", "sonnet"]


def _isolate_cli(monkeypatch):
    """Neutralise the CLI's cost-circuit + telemetry so a test can focus on the
    accept-threading contract without a live state.db."""
    monkeypatch.setattr(L, "cost_circuit_open", lambda *a, **k: False)
    monkeypatch.setattr(L, "write_llm_calls_row", lambda *a, **k: None)
    monkeypatch.delenv("MO_SHAPE_CHECK", raising=False)
    monkeypatch.delenv("MO_FUSE_ENABLED", raising=False)


def test_custom_dispatch_fn_never_receives_accept_even_for_gated_node(monkeypatch, tmp_path):
    # A caller/test-supplied dispatch_fn keeps the historic 5-arg contract; a
    # strict signature makes an accidental accept= kwarg raise TypeError. Even
    # for a gated node ('planner'), the custom fn must be called cleanly.
    _isolate_cli(monkeypatch)
    calls = {"n": 0}

    def strict_dispatch(model, prompt, out_file, timeout_s, max_turns):
        calls["n"] += 1
        Path(out_file).write_text('{"ok": true}')
        return 0

    out = str(tmp_path / "out.txt")
    rc = L.llm_dispatch(_cli_argv("planner", out), dispatch_fn=strict_dispatch)
    assert rc == 0
    assert calls["n"] == 1


def test_default_dispatch_receives_accept_for_planner(monkeypatch, tmp_path):
    _isolate_cli(monkeypatch)
    captured = {}

    def spy(model, prompt, out_file, timeout_s, max_turns, accept="__MISSING__"):
        captured["accept"] = accept
        Path(out_file).write_text('{"ok": true}')
        return 0

    monkeypatch.setattr(L, "mo_llm_dispatch", spy)
    out = str(tmp_path / "out.txt")
    L.llm_dispatch(_cli_argv("planner", out), dispatch_fn=None)
    assert captured["accept"] is looks_like_json


def test_default_dispatch_omits_accept_for_prose_node(monkeypatch, tmp_path):
    _isolate_cli(monkeypatch)
    captured = {}

    def spy(model, prompt, out_file, timeout_s, max_turns, accept="__MISSING__"):
        captured["accept"] = accept
        Path(out_file).write_text("free-form prose is fine here")
        return 0

    monkeypatch.setattr(L, "mo_llm_dispatch", spy)
    out = str(tmp_path / "out.txt")
    L.llm_dispatch(_cli_argv("researcher", out), dispatch_fn=None)
    # researcher isn't a JSON node → accept_fn is None → kwarg is not passed.
    assert captured["accept"] == "__MISSING__"
