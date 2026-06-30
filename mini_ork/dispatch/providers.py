"""Provider registry + telemetry parsers for the Python dispatch layer.

Maps a model lane to the command that runs it and the parsers that turn its
output into typed telemetry. Faithful port of the harvest logic in
lib/providers/cl_codex.sh (the codex JSONL usage/cost parse). The lane wrappers
are reused as the command for now; making them read the prompt from stdin (so
the whole chain is E2BIG-proof end-to-end, not just the Python boundary) is the
next migration slice.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .core import dispatch
from .models import DispatchRequest, DispatchResult, TokenUsage

# Lanes mini-ork knows about. codex/gemini are executable wrappers; the rest are
# Anthropic-compatible (claude CLI behind an env-pinned gateway).
KNOWN_MODELS = frozenset(
    {"opus", "sonnet", "kimi", "glm", "minimax", "codex", "gemini", "deepseek"}
)

# Codex list-price defaults (USD per million tokens), matching cl_codex.sh.
_CODEX_USD_PER_MTOK_IN = 1.25
_CODEX_USD_PER_MTOK_CACHED = 0.125
_CODEX_USD_PER_MTOK_OUT = 10.0


def parse_codex_usage(stdout: str) -> TokenUsage:
    """Sum token usage from codex's ``--json`` JSONL event stream.

    Faithful port of cl_codex.sh: accumulate ``turn.completed`` usage across all
    turns. Non-JSON / malformed lines are skipped, never fatal.
    """
    in_tok = out_tok = cached_tok = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            in_tok += int(u.get("input_tokens") or 0)
            out_tok += int(u.get("output_tokens") or 0)
            cached_tok += int(u.get("cached_input_tokens") or 0)
    return TokenUsage(
        input_tokens=in_tok, output_tokens=out_tok, cached_input_tokens=cached_tok
    )


def codex_cost(_stdout: str, usage: TokenUsage) -> float:
    """Estimate codex cost from token usage at list price. ``input_tokens``
    already INCLUDES the cached tokens (which bill at the discounted rate), so
    subtract them before applying the full input rate — same as cl_codex.sh."""
    fresh_in = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (
        fresh_in * _CODEX_USD_PER_MTOK_IN
        + usage.cached_input_tokens * _CODEX_USD_PER_MTOK_CACHED
        + usage.output_tokens * _CODEX_USD_PER_MTOK_OUT
    ) / 1e6


@dataclass(frozen=True)
class ProviderSpec:
    """How to run one model lane and read its telemetry."""

    model: str
    command: tuple[str, ...]
    parse_usage: object | None = None  # UsageParser | None
    parse_cost: object | None = None  # CostParser | None


def mini_ork_root(root: str | os.PathLike[str] | None = None) -> Path:
    """Repo root: explicit arg → $MINI_ORK_ROOT → package parent."""
    if root is not None:
        return Path(root).resolve()
    env = os.environ.get("MINI_ORK_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


def resolve_provider(
    model: str, root: str | os.PathLike[str] | None = None
) -> ProviderSpec:
    """Resolve a model lane to its :class:`ProviderSpec`.

    Raises ``ValueError`` for an unknown lane (we never invent a provider) and
    ``FileNotFoundError`` if the wrapper script is missing.
    """
    if model not in KNOWN_MODELS:
        raise ValueError(f"unknown model lane: {model!r}")
    wrapper = mini_ork_root(root) / "lib" / "providers" / f"cl_{model}.sh"
    if not wrapper.is_file():
        raise FileNotFoundError(f"provider wrapper not found: {wrapper}")
    command: tuple[str, ...] = (str(wrapper), "--print", "--output-format", "text")
    if model == "codex":
        return ProviderSpec(model, command, parse_codex_usage, codex_cost)
    return ProviderSpec(model, command)


def dispatch_model(
    request: DispatchRequest, root: str | os.PathLike[str] | None = None
) -> DispatchResult:
    """Resolve ``request.model`` to a provider and dispatch it. Unknown lane /
    missing wrapper come back as a structured ``ok=False`` result, not a raise."""
    try:
        spec = resolve_provider(request.model, root)
    except (ValueError, FileNotFoundError) as exc:
        return DispatchResult(ok=False, rc=2, error=str(exc), model=request.model)
    return dispatch(
        request,
        spec.command,
        parse_usage=spec.parse_usage,  # type: ignore[arg-type]
        parse_cost=spec.parse_cost,  # type: ignore[arg-type]
    )


def dispatch_with_command(
    request: DispatchRequest,
    command: Sequence[str],
    *,
    parse_usage: object | None = None,
    parse_cost: object | None = None,
) -> DispatchResult:
    """Escape hatch for tests / custom commands: dispatch an explicit argv."""
    return dispatch(request, command, parse_usage=parse_usage, parse_cost=parse_cost)  # type: ignore[arg-type]
