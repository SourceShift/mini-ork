"""Tests for the pre-dispatch lane-health gate (mini_ork.dispatch).

Targets the most-repeated prod failure: a lane whose API key is missing dies
SILENTLY and the run stalls for ~19 minutes. lane_health turns that into an
instant, clear failure; dispatch_model fails fast instead of dispatching.
"""

from __future__ import annotations

import stat

from mini_ork.dispatch import (
    DispatchRequest,
    dispatch_model,
    lane_health,
    preflight,
)

# A gateway wrapper that REQUIRES a key (the `${KEY:?}` guard), like cl_glm.sh.
KEYED_WRAPPER = '#!/usr/bin/env bash\nexport ANTHROPIC_AUTH_TOKEN="${GLM_API_KEY:?GLM_API_KEY required}"\n'
# An ambient wrapper with NO required key, like opus (uses the claude login).
AMBIENT_WRAPPER = '#!/usr/bin/env bash\nexport CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\n'


def _wrapper(tmp_path, model, body):
    prov = tmp_path / "lib" / "providers"
    prov.mkdir(parents=True, exist_ok=True)
    w = prov / f"cl_{model}.sh"
    w.write_text(body)
    w.chmod(w.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def test_missing_key_is_unhealthy_with_clear_reason(tmp_path, monkeypatch):
    _wrapper(tmp_path, "glm", KEYED_WRAPPER)
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    h = lane_health("glm")
    assert h.ok is False
    assert "GLM_API_KEY" in h.reason and "silently" in h.reason


def test_present_key_is_healthy(tmp_path, monkeypatch):
    _wrapper(tmp_path, "glm", KEYED_WRAPPER)
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.setenv("GLM_API_KEY", "set")
    assert lane_health("glm").ok is True


def test_ambient_lane_with_no_key_is_healthy(tmp_path, monkeypatch):
    _wrapper(tmp_path, "opus", AMBIENT_WRAPPER)
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    assert lane_health("opus").ok is True  # no ${KEY:?} declared → fine


def test_unknown_lane_and_missing_wrapper_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))  # empty: no wrappers
    assert lane_health("not-a-lane").ok is False
    assert lane_health("glm").ok is False  # known lane but wrapper absent


def test_dispatch_model_fails_fast_on_missing_key(tmp_path, monkeypatch):
    _wrapper(tmp_path, "glm", KEYED_WRAPPER)
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    res = dispatch_model(DispatchRequest(model="glm", prompt="hi"))
    assert res.ok is False
    assert res.rc == 2
    assert "preflight failed" in res.error and "GLM_API_KEY" in res.error


def test_preflight_check_can_be_disabled(tmp_path, monkeypatch):
    # Use a lane with NO wrapper so neither path makes a live call. Gate ON →
    # the preflight catches the missing wrapper; gate OFF → it skips the gate and
    # fails later at resolve (a different message). Proves the gate is opt-out.
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))  # empty: no wrappers
    on = dispatch_model(DispatchRequest(model="glm", prompt="hi"))
    assert "preflight failed" in on.error
    off = dispatch_model(
        DispatchRequest(model="glm", prompt="hi"), preflight_check=False
    )
    assert off.ok is False
    assert "preflight failed" not in off.error  # gate skipped; failed at resolve


def test_preflight_reports_all_lanes(tmp_path, monkeypatch):
    _wrapper(tmp_path, "glm", KEYED_WRAPPER)
    _wrapper(tmp_path, "opus", AMBIENT_WRAPPER)
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    report = preflight(["glm", "opus", "glm"])  # dup deduped
    assert set(report) == {"glm", "opus"}
    assert report["glm"].ok is False
    assert report["opus"].ok is True
