"""JSON-contract nodes (the planner) must not die on a wrong-SHAPE lane reply.

Regression guard for the 2026-10-09 incident: the planner is dispatched by the
Python plan runtime (``cli/plan.py`` -> ``llm_dispatch``), which never published
``MO_DISPATCH_CHAIN`` (the planner early-handler no-ops), so it ran a SINGLE
lane. An intermittently shape-rejecting glm planner (~2 of 3 calls) then failed
the whole run with rc=SHAPE_REJECT_RC at plan.py's ``rc != 0`` branch — before
its repair loop, which only ever sees an rc==0 plan with wrong *content*.

The fix (``mini_ork/dispatch/llm_dispatch.py``): when a boundary shape predicate
is armed, the lead lane is joined with the role's fallback tail so
``dispatch_with_fallback`` can hand off on a wrong shape; and a shape reject
re-enters the existing backoff loop instead of falling through to terminal.

These tests use the ``dispatch_fn`` seam, so no live provider is touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.dispatch import llm_dispatch as L
from mini_ork.dispatch.predicates import SHAPE_REJECT_RC
from mini_ork.dispatch.routing import dispatch_chain


def _argv_no_model(node_type: str, out: str) -> list[str]:
    """No ``--model``: the lane is resolved from policy (the incident path)."""
    return ["--node-type", node_type, "--prompt-text", "x", "--out", out]


def _argv_with_model(node_type: str, out: str, model: str) -> list[str]:
    return ["--node-type", node_type, "--prompt-text", "x", "--out", out,
            "--model", model]


def _isolate(monkeypatch):
    """Neutralise the cost circuit + telemetry so a test needs no live state.db."""
    monkeypatch.setattr(L, "cost_circuit_open", lambda *a, **k: False)
    monkeypatch.setattr(L, "write_llm_calls_row", lambda *a, **k: None)
    monkeypatch.delenv("MO_SHAPE_CHECK", raising=False)
    monkeypatch.delenv("MO_FUSE_ENABLED", raising=False)
    monkeypatch.delenv("MO_DISPATCH_MAX_ATTEMPTS", raising=False)
    # Deterministic policy: the resolved lead + a fixed tail.
    monkeypatch.setattr(L, "resolve_lane_model", lambda *a, **k: "glm")
    monkeypatch.setattr(L, "resolve_lane_family", lambda lane, *a, **k: lane)
    monkeypatch.setenv("MO_FALLBACK_CODING", "minimax,codex,sonnet")


def test_planner_without_model_override_gets_fallback_chain(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    seen = {}

    def spy(model, prompt, out_file, timeout_s, max_turns):
        seen["model"] = model
        Path(out_file).write_text('{"ok": true}')
        return 0

    out = str(tmp_path / "out.txt")
    rc = L.llm_dispatch(_argv_no_model("planner", out), dispatch_fn=spy)
    assert rc == 0
    assert seen["model"] == dispatch_chain("planner", "glm")
    assert "," in seen["model"]  # a chain, not a lone lane


def test_prose_node_without_model_override_stays_single_lane(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    seen = {}

    def spy(model, prompt, out_file, timeout_s, max_turns):
        seen["model"] = model
        Path(out_file).write_text("free-form prose is fine here")
        return 0

    out = str(tmp_path / "out.txt")
    rc = L.llm_dispatch(_argv_no_model("researcher", out), dispatch_fn=spy)
    assert rc == 0
    assert seen["model"] == "glm"  # no predicate armed -> no chain


def test_explicit_model_override_is_not_chained(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    seen = {}

    def spy(model, prompt, out_file, timeout_s, max_turns):
        seen["model"] = model
        Path(out_file).write_text('{"ok": true}')
        return 0

    out = str(tmp_path / "out.txt")
    rc = L.llm_dispatch(
        _argv_with_model("planner", out, "sonnet"), dispatch_fn=spy)
    assert rc == 0
    assert seen["model"] == "sonnet"  # an operator pin is left untouched


def test_shape_reject_retries_within_max_attempts(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    monkeypatch.setattr(L, "backoff_seconds", lambda n: 0)
    monkeypatch.setenv("MO_DISPATCH_MAX_ATTEMPTS", "2")
    calls = {"n": 0}

    def reject(model, prompt, out_file, timeout_s, max_turns):
        calls["n"] += 1
        Path(out_file).write_text("not json at all")  # wrong shape
        return SHAPE_REJECT_RC

    out = str(tmp_path / "out.txt")
    rc = L.llm_dispatch(_argv_no_model("planner", out), dispatch_fn=reject)
    assert rc == SHAPE_REJECT_RC
    assert calls["n"] == 2  # retried once, then gave up at the bound


def test_shape_reject_recovers_on_retry(monkeypatch, tmp_path):
    _isolate(monkeypatch)
    monkeypatch.setattr(L, "backoff_seconds", lambda n: 0)
    monkeypatch.setenv("MO_DISPATCH_MAX_ATTEMPTS", "3")
    calls = {"n": 0}

    def flaky(model, prompt, out_file, timeout_s, max_turns):
        calls["n"] += 1
        if calls["n"] == 1:
            Path(out_file).write_text("prose, not json")
            return SHAPE_REJECT_RC
        Path(out_file).write_text('{"ok": true}')
        return 0

    out = str(tmp_path / "out.txt")
    rc = L.llm_dispatch(_argv_no_model("planner", out), dispatch_fn=flaky)
    assert rc == 0
    assert Path(out).read_text() == '{"ok": true}'


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
