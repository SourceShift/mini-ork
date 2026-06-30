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
    KNOWN_MODELS,
    ProviderSpec,
    codex_cost,
    dispatch_model,
    dispatch_with_command,
    parse_codex_usage,
    resolve_provider,
)

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
    "KNOWN_MODELS",
]
