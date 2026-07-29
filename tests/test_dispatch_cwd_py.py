"""Tests for the cwd guard (mini_ork.dispatch) — the dispatch-side half of the
isolation fix. A target-repo lane must never run with its cwd inside the
mini-ork framework tree; that confusion is how a consuming repo's provider git
ops (codex's refs/codex resets) corrupt the framework repo.
"""

from __future__ import annotations

import os

from mini_ork.dispatch import (
    DispatchRequest,
    cwd_guard,
    dispatch_model,
    resolve_target_cwd,
)

def _providers_registry(root):
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "providers.yaml").write_text(
        "providers:\n"
        "  glm:\n"
        "    kind: anthropic-compat\n"
        "    family: zai\n"
        "    api_key_env: GLM_API_KEY\n"
        "    base_url: https://api.z.ai/api/anthropic\n"
        "    model: glm-4.7\n",
        encoding="utf-8",
    )


def test_resolve_target_cwd_precedence(tmp_path, monkeypatch):
    # explicit request.cwd wins
    r = DispatchRequest(model="glm", prompt="x", cwd=str(tmp_path / "a"))
    assert resolve_target_cwd(r) == os.path.abspath(str(tmp_path / "a"))
    # then MO_TARGET_CWD
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path / "b"))
    assert resolve_target_cwd(DispatchRequest(model="glm", prompt="x")) == os.path.abspath(
        str(tmp_path / "b")
    )


def test_cwd_inside_framework_is_rejected(tmp_path, monkeypatch):
    # Clear the opt-in if it leaked in from the CALLER's environment — a
    # framework-edit run (MO_ALLOW_FRAMEWORK_CWD=1) invoking the whole test
    # suite via verifiers/test.sh would otherwise flip this guard to ok=True
    # and poison the suite gate for every run (2026-07-03 migration batch).
    monkeypatch.delenv("MO_ALLOW_FRAMEWORK_CWD", raising=False)
    framework = tmp_path / "mini-ork"
    (framework / "sub").mkdir(parents=True)
    g = cwd_guard(str(framework / "sub"), root=framework)
    assert g.ok is False
    assert "framework tree" in g.reason and "cwd-confusion" in g.reason


def test_cwd_outside_framework_is_allowed(tmp_path):
    framework = tmp_path / "mini-ork"
    framework.mkdir()
    target = tmp_path / "researcher"
    target.mkdir()
    assert cwd_guard(str(target), root=framework).ok is True


def test_framework_cwd_allowed_with_optin(tmp_path, monkeypatch):
    framework = tmp_path / "mini-ork"
    framework.mkdir()
    monkeypatch.setenv("MO_ALLOW_FRAMEWORK_CWD", "1")
    assert cwd_guard(str(framework), root=framework).ok is True  # self-edit opt-in


def test_dispatch_model_fails_fast_on_framework_cwd(tmp_path, monkeypatch):
    # Healthy lane (key set) but the requested cwd is inside the framework tree
    # → dispatch_model must refuse BEFORE running the provider.
    _providers_registry(tmp_path)
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    monkeypatch.setenv("GLM_API_KEY", "set")
    monkeypatch.delenv("MO_ALLOW_FRAMEWORK_CWD", raising=False)
    res = dispatch_model(
        DispatchRequest(model="glm", prompt="hi", cwd=str(tmp_path / "lib"))
    )
    assert res.ok is False
    assert res.rc == 2
    assert "cwd guard failed" in res.error
