"""An anthropic-native lane cannot authenticate on a remote node without a
portable credential, so the remote spawn is refused before any network call.

Its auth is the operator's local Claude login (the macOS Keychain), which never
travels. Spawned anyway, the CLI ran its default model with no key; the
"Invalid API key" was classified ``auth`` and pinned by triage and the lane
fuse on the lane alias that requested it (Daytona e2e, run-1791910825-57872).
The refusal must read as a setup (``config``) failure, never ``auth``."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mini_ork.dispatch import providers as P
from mini_ork.dispatch.llm_dispatch import classify_error
from mini_ork.dispatch.models import DispatchResult


@pytest.fixture
def registry(monkeypatch):
    reg = {
        "opus": {"kind": "anthropic-native"},
        "glm": {"kind": "anthropic-compat", "api_key_env": "GLM_API_KEY"},
    }
    monkeypatch.setattr(P, "_load_providers_registry", lambda root=None: reg)
    return reg


def _env(tmp_path, **extra):
    # no MO_NODE_ENV → no profile gate; secret store pinned to an absent file
    return {"MO_PLACEMENT": "remote", "MINI_ORK_HOME": str(tmp_path),
            "MINI_ORK_SECRETS": str(tmp_path / "absent.sh"), **extra}


def test_native_lane_without_portable_credential_is_refused_as_config(registry, tmp_path):
    out = P._remote_lane_secrets("opus", SimpleNamespace(env={}), _env(tmp_path), None)
    assert isinstance(out, DispatchResult) and not out.ok and out.rc == 2
    assert "claude setup-token" in out.error and "MO_PLACEMENT_LOCAL_ROLES" in out.error
    assert classify_error(out.error) == "config"        # never "auth": no false fuse


@pytest.mark.parametrize("key", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"])
def test_native_lane_with_a_portable_credential_passes(registry, tmp_path, key):
    out = P._remote_lane_secrets("opus", SimpleNamespace(env={}),
                                 _env(tmp_path, **{key: "sk-portable-0123456789"}), None)
    assert out == {key: "sk-portable-0123456789"}


def test_compat_lane_is_untouched(registry, tmp_path):
    out = P._remote_lane_secrets(
        "glm", SimpleNamespace(env={"ANTHROPIC_AUTH_TOKEN": "glm-token-0123456789"}),
        _env(tmp_path), None)
    assert out == {"ANTHROPIC_AUTH_TOKEN": "glm-token-0123456789"}

