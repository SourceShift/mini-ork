from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from mini_ork.dispatch import ProviderSpec, lane_health, resolve_provider


def _write_registry(root: Path, providers: dict) -> Path:
    path = root / "config" / "providers.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"providers": providers}), encoding="utf-8")
    return path


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\ncat\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _clear_registry_overrides(monkeypatch) -> None:
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    # Isolate MINI_ORK_HOME to a path with no providers.yaml. Deleting it would
    # fall back to the relative default ".mini-ork/config/providers.yaml", which
    # is a REAL file when the suite runs from the repo root and would shadow the
    # per-test tmp registry (root=tmp_path). Point it at a nonexistent path so
    # only the explicit root candidate resolves.
    monkeypatch.setenv("MINI_ORK_HOME", "/nonexistent-miniork-home-for-tests")


def test_registry_only_lane_resolves_to_provider_spec(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _write_registry(
        tmp_path,
        {
            "private_claude": {
                "kind": "anthropic-native",
                "model": "claude-opus-4-7",
            }
        },
    )

    spec = resolve_provider("private_claude", tmp_path)

    assert isinstance(spec, ProviderSpec)
    assert spec.model == "private_claude"
    assert spec.command[0] == "claude"
    assert spec.env["ANTHROPIC_MODEL"] == "claude-opus-4-7"


def test_registry_resolves_all_provider_kinds(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "native-key")
    monkeypatch.setenv("COMPAT_KEY", "compat-key")
    monkeypatch.setenv("OPENAI_TEST_KEY", "openai-key")
    local = _write_executable(tmp_path / "scripts" / "local-provider.sh")
    _write_registry(
        tmp_path,
        {
            "native": {
                "kind": "anthropic-native",
                "model": "claude-sonnet-4-6",
            },
            "gateway": {
                "kind": "anthropic-compat",
                "model": "gateway-model",
                "base_url": "https://gateway.example/anthropic",
                "api_key_env": "COMPAT_KEY",
                "extra_env": {"ENABLE_TOOL_SEARCH": "false"},
            },
            "openai": {
                "kind": "openai-compat",
                "model": "gpt-test",
                "base_url": "https://openai.example/v1",
                "api_key_env": "OPENAI_TEST_KEY",
            },
            "local": {
                "kind": "executable",
                "script": "scripts/local-provider.sh",
                "extra_env": {"LOCAL_MODE": "test"},
            },
        },
    )

    native = resolve_provider("native", tmp_path)
    gateway = resolve_provider("gateway", tmp_path)
    openai = resolve_provider("openai", tmp_path)
    executable = resolve_provider("local", tmp_path)

    assert native.command[0] == "claude"
    assert native.env["ANTHROPIC_MODEL"] == "claude-sonnet-4-6"
    assert gateway.env["ANTHROPIC_BASE_URL"] == "https://gateway.example/anthropic"
    assert gateway.env["ANTHROPIC_AUTH_TOKEN"] == "compat-key"
    assert gateway.env["ENABLE_TOOL_SEARCH"] == "false"
    assert openai.command[:3] == (
        sys.executable,
        "-m",
        "mini_ork.dispatch.codex_transport",
    )
    assert openai.env["MO_OAI_MODEL"] == "gpt-test"
    assert openai.env["MO_OAI_ENV_KEY"] == "OPENAI_TEST_KEY"
    assert executable.command[0] == str(local)
    assert executable.env["LOCAL_MODE"] == "test"


def test_missing_base_url_names_field(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    _write_registry(
        tmp_path,
        {"broken": {"kind": "anthropic-compat", "api_key_env": "COMPAT_KEY"}},
    )

    with pytest.raises(ValueError, match="base_url"):
        resolve_provider("broken", tmp_path)


def test_missing_api_key_env_names_field(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    _write_registry(
        tmp_path,
        {"broken": {"kind": "openai-compat", "base_url": "https://api.example/v1"}},
    )

    with pytest.raises(ValueError, match="api_key_env"):
        resolve_provider("broken", tmp_path)


def test_named_codex_registry_lane_uses_native_transport(tmp_path, monkeypatch):
    """A registry-defined Codex lane configures the native transport directly."""
    _clear_registry_overrides(monkeypatch)
    _write_registry(
        tmp_path,
        {
            "codex": {
                "kind": "openai-compat",
                "model": "shadow-model",
                "base_url": "https://shadow.example/v1",
                "api_key_env": "SHADOW_KEY",
            }
        },
    )

    spec = resolve_provider("codex", tmp_path)

    assert "mini_ork.dispatch.codex_transport" in spec.command
    assert spec.env["MO_OAI_BASE_URL"] == "https://shadow.example/v1"
    assert spec.env["MO_OAI_MODEL"] == "shadow-model"


def test_registry_lane_health_is_runnable_with_key(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    monkeypatch.setenv("PRIVATE_KEY", "set")
    _write_registry(
        tmp_path,
        {
            "private_openai": {
                "kind": "openai-compat",
                "base_url": "https://api.example/v1",
                "api_key_env": "PRIVATE_KEY",
            }
        },
    )

    assert lane_health("private_openai", tmp_path).ok is True


def test_registry_lane_health_names_unset_key(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    _write_registry(
        tmp_path,
        {
            "private_openai": {
                "kind": "openai-compat",
                "base_url": "https://api.example/v1",
                "api_key_env": "PRIVATE_KEY",
            }
        },
    )

    health = lane_health("private_openai", tmp_path)

    assert health.ok is False
    assert "PRIVATE_KEY" in health.reason
    assert "not set" in health.reason


def test_registry_file_resolution_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    override = tmp_path / "override.yaml"
    _write_registry(repo, {"lane": {"kind": "anthropic-native", "model": "repo"}})
    _write_registry(home, {"lane": {"kind": "anthropic-native", "model": "home"}})
    override.write_text(
        yaml.safe_dump(
            {"providers": {"lane": {"kind": "anthropic-native", "model": "override"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(override))

    assert resolve_provider("lane", repo).env["ANTHROPIC_MODEL"] == "override"
    monkeypatch.delenv("MINI_ORK_PROVIDERS")
    assert resolve_provider("lane", repo).env["ANTHROPIC_MODEL"] == "home"
