"""mini_ork.dispatch — typed Python dispatch layer (Phase-0 migration, ADR-001).

The first ported slice of lib/llm-dispatch.sh. Delivers prompts to providers
over stdin (no E2BIG) and returns typed results with faithful exit codes —
structurally retiring the two bash failure classes (ARG_MAX, rc-masking).
Coexists with the bash dispatch (strangler-fig); not yet wired as the default.
"""

from __future__ import annotations

from .core import dispatch
from .models import DispatchRequest, DispatchResult, TokenUsage
from .providers import (
    EXECUTABLE_MODELS,
    KNOWN_MODELS,
    LaneHealth,
    ProviderSpec,
    claude_cost,
    claude_env_for,
    claude_result_text,
    codex_cost,
    cwd_guard,
    dispatch_model,
    dispatch_with_command,
    lane_health,
    parse_claude_usage,
    parse_codex_usage,
    preflight,
    provider_for_model,
    resolve_provider,
    resolve_target_cwd,
)
from .telemetry import cache_aware_cost, persist_call

__all__ = [
    "dispatch",
    "dispatch_model",
    "dispatch_with_command",
    "DispatchRequest",
    "DispatchResult",
    "TokenUsage",
    "ProviderSpec",
    "resolve_provider",
    "parse_codex_usage",
    "codex_cost",
    "parse_claude_usage",
    "claude_cost",
    "claude_result_text",
    "claude_env_for",
    "KNOWN_MODELS",
    "EXECUTABLE_MODELS",
    "persist_call",
    "cache_aware_cost",
    "lane_health",
    "preflight",
    "LaneHealth",
    "provider_for_model",
    "cwd_guard",
    "resolve_target_cwd",
]
