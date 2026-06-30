"""Typed data contracts for the Python dispatch layer (Phase-0 migration, ADR-001).

Stdlib dataclasses (not pydantic) so the dispatch core stays dependency-free —
it must be importable without the web stack. These replace the untyped
jq/heredoc JSON shuffling of lib/llm-dispatch.sh: the LLM envelope becomes a
typed return value instead of env-var side-channels.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TokenUsage:
    """Per-call token totals harvested from a provider's output stream."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class DispatchRequest:
    """A single model call. The prompt is delivered to the provider over STDIN
    (see core.dispatch), never argv/env — so a multi-MB prompt can never hit
    ARG_MAX/E2BIG, the failure that killed the bash codex lane fleet-wide."""

    model: str
    prompt: str
    timeout_s: float = 1500.0
    max_turns: int = 60
    # Extra environment for the provider process. The prompt is NEVER put here.
    env: dict[str, str] = field(default_factory=dict)
    # Working directory for the provider process. None = inherit the caller's
    # cwd. The cwd guard (providers.cwd_guard) refuses a dispatch whose cwd lands
    # inside the mini-ork framework tree — that is the cwd-confusion that lets a
    # target-repo lane's git ops corrupt the framework repo.
    cwd: str | None = None


@dataclass
class DispatchResult:
    """The outcome of a dispatch. `rc` is propagated faithfully from the
    provider process — there is no `if cmd; then…; fi` construct that can mask a
    non-zero exit as success (the bash D-013/D-014 regression)."""

    ok: bool
    rc: int
    text: str = ""
    error: str = ""
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    duration_ms: int = 0
