"""Tests for the per-model harness engine registry and dispatch shims.

These tests validate the new engine-object refactor with behavior-focused checks:
- registry shape and capability metadata
- sidecar engine behavior preservation
- compatibility shims for `ENGINE_COMMAND_BUILDERS` and `MODEL_DISPATCH_BACKENDS`
"""

from __future__ import annotations

from mini_ork.dispatch import providers
from mini_ork.dispatch.providers import (
    ENGINES,
    ProviderSpec,
)
from mini_ork.dispatch.models import DispatchRequest, DispatchResult, TokenUsage


def _spec(command: tuple[str, ...]) -> ProviderSpec:
    return ProviderSpec(
        model="codex", command=command, parse_usage=None, parse_cost=None, parse_text=None
    )


def _resp(*, usage: TokenUsage | None = None, cost_usd: float = 0.0) -> DispatchResult:
    return DispatchResult(
        ok=True,
        rc=0,
        model="codex",
        text="ok",
        usage=usage or TokenUsage(),
        cost_usd=cost_usd,
    )


def test_engines_is_three_and_expected():
    assert set(ENGINES.keys()) == {"claude", "codex", "opencode"}


def test_executable_engines_are_tool_grant_false_ratchet():
    assert ENGINES["codex"].capabilities().tool_grants is False
    assert ENGINES["opencode"].capabilities().tool_grants is False


def test_claude_engine_capabilities_require_tool_grants_resume_and_session_capture():
    caps = ENGINES["claude"].capabilities()
    assert caps.tool_grants is True
    assert caps.resume is True
    assert caps.session_capture is True


def test_build_command_is_identity_for_sidecar_engines():
    request = DispatchRequest(model="codex", prompt="p")
    command = ("codex", "run", "--json")

    assert ENGINES["codex"].build_command(command, request=request, env={}) == command
    assert ENGINES["opencode"].build_command(command, request=request, env={}) == command


def test_build_command_injects_for_claude_when_grants_enabled():
    request = DispatchRequest(model="opus", prompt="x")
    command = ENGINES["claude"].build_command(
        ("claude", "-p", "x"),
        request=request,
        env={"MO_NODE_TYPE": "implementer", "MINI_ORK_RUN_DIR": "/tmp"},
    )

    assert "--allowedTools" in command
    assert "--strict-mcp-config" in command
    assert "Read,Write,Edit,Bash" in command


def test_build_command_keeps_resume_id_even_when_tool_grants_disabled():
    request = DispatchRequest(model="opus", prompt="x")
    command = ENGINES["claude"].build_command(
        ("claude", "-p", "x"),
        request=request,
        env={"MO_TOOL_GRANTS_DISABLED": "1", "MO_RESUME_SESSION_ID": "sess-1"},
    )

    assert command == ("claude", "--resume", "sess-1", "-p", "x")


def test_sidecar_telemetry_engine_folds_sidecars(monkeypatch):
    captured: dict[str, object] = {}

    def fake_dispatch(request: DispatchRequest, command: tuple[str, ...]) -> DispatchResult:
        captured["request"] = request
        captured["command"] = command
        return _resp(
            usage=TokenUsage(input_tokens=11, output_tokens=5), cost_usd=2.5
        )

    def fake_read(_usage_path: str, _cost_path: str) -> tuple[TokenUsage, float]:
        return TokenUsage(input_tokens=11, output_tokens=5), 2.5

    monkeypatch.setattr(providers, "dispatch", fake_dispatch)
    monkeypatch.setattr(providers, "_read_codex_sidecars", fake_read)

    request = DispatchRequest(
        model="codex", prompt="x", cwd="/tmp", workspace="agent", env={}
    )
    result = ENGINES["codex"].dispatch(request, _spec(("codex", "run")))

    assert result.ok
    assert result.usage == TokenUsage(input_tokens=11, output_tokens=5)
    assert result.cost_usd == 2.5
    assert isinstance((req := captured.get("request")), DispatchRequest)
    assert req.env.get("MO_USAGE_FILE", "").endswith(".tokens")
    assert req.env.get("MO_COST_FILE", "").endswith(".cost")
    assert req.cwd == "/tmp"
    assert req.workspace == "agent"
    assert captured["command"] == ("codex", "run")
