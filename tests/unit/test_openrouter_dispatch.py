"""Contracts for the ``openai-chat`` provider kind and its OpenRouter lane.

Two layers:

* network-free unit tests exercise ``openai_chat_transport.run`` with a mocked
  ``urlopen`` — proving the chat-completions envelope parse, the null-content
  guard (reasoning models), and credential/HTTP failure codes;
* one opt-in live smoke (``MO_LIVE_OPENROUTER_SMOKE=1`` + a resolvable key)
  drives the real ``openrouter`` lane end-to-end through ``dispatch_model``.
  CI never sets the flag, so the network path stays out of the default suite.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from mini_ork.dispatch import openai_chat_transport as transport
from mini_ork.dispatch.providers import (
    dispatch_model,
    provider_environment,
    required_secret_envs,
    resolve_provider,
)
from mini_ork.dispatch.core import DispatchRequest

REPO = Path(__file__).resolve().parents[2]


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _chat_env(monkeypatch, **overrides: str) -> None:
    monkeypatch.setenv("MO_OAI_BASE_URL", overrides.get("base_url", "https://x.example/v1"))
    monkeypatch.setenv("MO_OAI_MODEL", overrides.get("model", "stealth/ox-alpha"))
    monkeypatch.setenv("MO_OAI_ENV_KEY", "OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY", overrides.get("key", "sk-test"))


def test_transport_prints_assistant_content(monkeypatch, capsys):
    _chat_env(monkeypatch)
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            json.dumps({"choices": [{"message": {"content": "OK"}}]})
        )

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.stdin", io.StringIO("Say OK"))

    rc = transport.run(["--print", "--output-format", "text"])

    assert rc == 0
    assert capsys.readouterr().out == "OK\n"
    assert captured["url"] == "https://x.example/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-test"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "stealth/ox-alpha"
    assert payload["messages"] == [{"role": "user", "content": "Say OK"}]


def test_transport_flags_empty_reasoning_content(monkeypatch, capsys):
    _chat_env(monkeypatch)
    monkeypatch.setattr(
        transport.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            json.dumps({"choices": [{"message": {"content": None}}]})
        ),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    rc = transport.run([])

    assert rc == 6
    assert "MO_OAI_MAX_TOKENS" in capsys.readouterr().err


def test_transport_requires_key(monkeypatch, capsys):
    _chat_env(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    rc = transport.run([])

    assert rc == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_openai_chat_kind_resolves_to_http_transport(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    registry = tmp_path / ".mini-ork" / "config"
    registry.mkdir(parents=True)
    (registry / "providers.yaml").write_text(
        "providers:\n"
        "  openrouter:\n"
        "    kind: openai-chat\n"
        "    model: stealth/ox-alpha\n"
        "    base_url: https://openrouter.ai/api/v1\n"
        "    api_key_env: OPENROUTER_API_KEY\n"
        "    extra_env:\n"
        '      MO_OAI_MAX_TOKENS: "2048"\n',
        encoding="utf-8",
    )

    spec = resolve_provider("openrouter", tmp_path)

    assert spec.command[0] == sys.executable
    assert spec.command[1].endswith("openai_chat_transport.py")
    assert spec.env["MO_OAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert spec.env["MO_OAI_MODEL"] == "stealth/ox-alpha"
    assert spec.env["MO_OAI_ENV_KEY"] == "OPENROUTER_API_KEY"
    assert spec.env["MO_OAI_MAX_TOKENS"] == "2048"
    assert spec.env["OPENROUTER_API_KEY"] == "sk-test"
    assert required_secret_envs("openrouter", tmp_path) == ("OPENROUTER_API_KEY",)


def _openrouter_key_available() -> bool:
    try:
        env = provider_environment()
    except Exception:  # noqa: BLE001 — availability probe, never fatal
        return False
    return bool(env.get("OPENROUTER_API_KEY"))


@pytest.mark.skipif(
    os.environ.get("MO_LIVE_OPENROUTER_SMOKE") != "1" or not _openrouter_key_available(),
    reason="live smoke: set MO_LIVE_OPENROUTER_SMOKE=1 with a resolvable OPENROUTER_API_KEY",
)
def test_openrouter_live_dispatch_end_to_end():
    result = dispatch_model(
        DispatchRequest(
            model="openrouter",
            prompt="Reply with the single word: OK",
            timeout_s=60,
            env={"MO_TARGET_CWD": "/tmp"},
        ),
        REPO,
    )

    assert result.ok, result.error
    assert result.rc == 0
    assert result.text.strip()
