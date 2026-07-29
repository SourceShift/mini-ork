"""Tests for the pre-dispatch lane-health gate (mini_ork.dispatch).

Targets the most-repeated prod failure: a lane whose API key is missing dies
SILENTLY and the run stalls for ~19 minutes. lane_health turns that into an
instant, clear failure; dispatch_model fails fast instead of dispatching.
"""

from __future__ import annotations

from pathlib import Path

from mini_ork.dispatch import (
    DispatchRequest,
    dispatch_model,
    lane_health,
    preflight,
)

REPO = Path(__file__).resolve().parents[1]

def test_missing_key_is_unhealthy_with_clear_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    h = lane_health("glm")
    assert h.ok is False
    assert "GLM_API_KEY" in h.reason and "silently" in h.reason


def test_present_key_is_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.setenv("GLM_API_KEY", "set")
    assert lane_health("glm").ok is True


def test_ambient_lane_with_no_key_is_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    assert lane_health("opus").ok is True


def test_unknown_lane_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    assert lane_health("not-a-lane").ok is False


def test_dispatch_model_fails_fast_on_missing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    res = dispatch_model(DispatchRequest(model="glm", prompt="hi"))
    assert res.ok is False
    assert res.rc == 2
    assert "preflight failed" in res.error and "GLM_API_KEY" in res.error


def test_preflight_check_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    on = dispatch_model(DispatchRequest(model="not-a-lane", prompt="hi"))
    assert "preflight failed" in on.error
    off = dispatch_model(
        DispatchRequest(model="not-a-lane", prompt="hi"), preflight_check=False
    )
    assert off.ok is False
    assert "preflight failed" not in off.error  # gate skipped; failed at resolve


def test_preflight_reports_all_lanes(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(REPO))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    report = preflight(["glm", "opus", "glm"])  # dup deduped
    assert set(report) == {"glm", "opus"}
    assert report["glm"].ok is False
    assert report["opus"].ok is True
