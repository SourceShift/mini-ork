"""Lane-fallback resilience — the fix for the recurring 'one hung lane stalls
the whole run' failure. A dead/hung primary lane must be abandoned and the run
served by the next lane, instead of blocking for the full 25-min timeout.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.dispatch.models import DispatchRequest  # noqa: E402
from mini_ork.dispatch.providers import dispatch_with_fallback  # noqa: E402


def test_dead_primary_falls_back_to_working_lane(monkeypatch):
    # Force glm 'dead' (unset key → preflight fails instantly, standing in for a
    # hang). The chain must fall back to codex (works in this env) and succeed.
    monkeypatch.setenv("GLM_API_KEY", "")
    req = DispatchRequest(model="glm", prompt="Reply with exactly one word: OK",
                          timeout_s=120, cwd="/tmp")  # /tmp: outside framework, cwd_guard ok
    r = dispatch_with_fallback(req, ["glm", "codex"], per_attempt_timeout_s=110)
    assert r.ok, f"expected fallback to codex to succeed, got rc={r.rc} {r.error}"
    assert (r.text or "").strip(), "served lane returned empty output"


def test_all_lanes_dead_returns_faithful_failure_not_hang(monkeypatch):
    # Both lanes dead → returns a faithful ok=False fast (no 25-min hang).
    monkeypatch.setenv("GLM_API_KEY", "")
    monkeypatch.setenv("KIMI_API_KEY", "")
    monkeypatch.setenv("MINIMAX_API_KEY", "")
    req = DispatchRequest(model="glm", prompt="x", timeout_s=10, cwd="/tmp")
    r = dispatch_with_fallback(req, ["glm", "kimi", "minimax"])
    assert not r.ok
    assert r.rc != 0
