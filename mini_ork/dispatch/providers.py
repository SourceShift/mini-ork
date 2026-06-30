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
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .core import dispatch
from .models import DispatchRequest, DispatchResult, TokenUsage

# Lanes mini-ork knows about. codex/gemini are EXECUTABLE wrappers (run the
# cl_*.sh as a command); the rest are the CLAUDE family (source the cl_*.sh to
# pin ANTHROPIC_* env, then run `claude --print --output-format json`).
EXECUTABLE_MODELS = frozenset({"codex", "gemini"})
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


# ── claude family (anthropic-compatible lanes) ──────────────────────────────
# `claude --print --output-format json` emits {"result", "total_cost_usd",
# "usage": {...}}. The body is .result (NOT raw stdout), cost is reported
# directly, and usage carries the cache-aware token split.


def _claude_envelope(stdout: str) -> dict:
    try:
        obj = json.loads(stdout)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def claude_result_text(stdout: str) -> str:
    """Extract the assistant body from claude's JSON envelope (.result)."""
    env = _claude_envelope(stdout)
    return str(env.get("result") or "") if env else stdout


def parse_claude_usage(stdout: str) -> TokenUsage:
    u = (_claude_envelope(stdout).get("usage")) or {}
    return TokenUsage(
        input_tokens=int(u.get("input_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or 0),
        cached_input_tokens=int(u.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(u.get("cache_creation_input_tokens") or 0),
    )


def claude_cost(stdout: str, _usage: TokenUsage) -> float:
    """Claude reports real billed cost in the envelope — trust it over an
    estimate."""
    try:
        return float(_claude_envelope(stdout).get("total_cost_usd") or 0.0)
    except (ValueError, TypeError):
        return 0.0


def claude_env_for(
    model: str, root: str | os.PathLike[str] | None = None
) -> dict[str, str]:
    """Capture the ANTHROPIC_*/CLAUDE_* env a claude-family wrapper exports, by
    sourcing it in a subshell. This reuses the committed cl_*.sh as the single
    source of truth for each lane's base_url/model/key-env — no duplication in
    Python. Returns {} if the wrapper's required API key env var is unset (its
    `${KEY:?}` guard aborts the source before any export runs)."""
    wrapper = mini_ork_root(root) / "lib" / "providers" / f"cl_{model}.sh"
    if not wrapper.is_file():
        raise FileNotFoundError(f"provider wrapper not found: {wrapper}")
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"source {shlex.quote(str(wrapper))} >/dev/null 2>&1; "
            'env | grep -E "^(ANTHROPIC_|CLAUDE_CODE_)" || true',
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    env: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key] = value
    return env


@dataclass(frozen=True)
class ProviderSpec:
    """How to run one model lane and read its telemetry."""

    model: str
    command: tuple[str, ...]
    parse_usage: object | None = None  # UsageParser | None
    parse_cost: object | None = None  # CostParser | None
    parse_text: object | None = None  # TextParser | None
    env: Mapping[str, str] = field(default_factory=dict)


# Provider family recorded in llm_calls.provider, per built-in lane.
_PROVIDER_FAMILY = {
    "opus": "anthropic",
    "sonnet": "anthropic",
    "codex": "openai",
    "gemini": "google",
    "glm": "zai",
    "kimi": "moonshot",
    "minimax": "minimax",
    "deepseek": "deepseek",
}


def provider_for_model(model: str) -> str:
    """Telemetry provider family for a lane (falls back to the lane name)."""
    return _PROVIDER_FAMILY.get(model, model)


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

    if model in EXECUTABLE_MODELS:
        # Run the wrapper as a command; prompt arrives over stdin (core.dispatch).
        # stdout is the CLEANED assistant text — cl_codex.sh redirects the codex
        # JSONL to a stream file and writes usage/cost to MO_*_FILE sidecars, so
        # there are no stdout parsers here; dispatch_model reads the sidecars.
        command: tuple[str, ...] = (str(wrapper), "--print", "--output-format", "text")
        return ProviderSpec(model, command)

    # Claude family: env-pin from the wrapper, then run claude with JSON output.
    return ProviderSpec(
        model=model,
        command=("claude", "--print", "--output-format", "json"),
        parse_usage=parse_claude_usage,
        parse_cost=claude_cost,
        parse_text=claude_result_text,
        env=claude_env_for(model, root),
    )


# A wrapper's `${SOME_API_KEY:?}` guard declares the key it requires. If that
# var is unset the bash wrapper aborts and the lane produces nothing — the exact
# "minimax died silently, 19-min stall" failure from prod sessions. We read that
# declaration to fail FAST with a clear reason instead of stalling.
_REQUIRED_KEY_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*_API_KEY)\s*:[?]")


@dataclass(frozen=True)
class LaneHealth:
    ok: bool
    reason: str


def lane_health(model: str, root: str | os.PathLike[str] | None = None) -> LaneHealth:
    """Cheap pre-dispatch check: is this lane runnable right now? Catches the
    common silent-death cause — a wrapper that requires an API key env var which
    isn't set. Does NOT make a network call. Lanes with no declared key (ambient
    auth, e.g. opus) are healthy as long as the wrapper exists."""
    if model not in KNOWN_MODELS:
        return LaneHealth(False, f"unknown lane: {model!r}")
    wrapper = mini_ork_root(root) / "lib" / "providers" / f"cl_{model}.sh"
    if not wrapper.is_file():
        return LaneHealth(False, f"wrapper missing: {wrapper}")
    try:
        text = wrapper.read_text(encoding="utf-8")
    except OSError as exc:
        return LaneHealth(False, f"wrapper unreadable: {exc}")
    for match in _REQUIRED_KEY_RE.finditer(text):
        key = match.group(1)
        if not os.environ.get(key):
            return LaneHealth(
                False, f"{model}: ${key} is not set — lane would die silently"
            )
    return LaneHealth(True, "ok")


def preflight(
    models: Sequence[str], root: str | os.PathLike[str] | None = None
) -> dict[str, LaneHealth]:
    """Health-check several lanes at once (e.g. every lane a recipe will use).
    Returns {model: LaneHealth}; callers abort the run if any are unhealthy."""
    return {m: lane_health(m, root) for m in dict.fromkeys(models)}


def dispatch_model(
    request: DispatchRequest,
    root: str | os.PathLike[str] | None = None,
    *,
    preflight_check: bool = True,
) -> DispatchResult:
    """Resolve ``request.model`` to a provider and dispatch it. Unknown lane /
    missing wrapper / a lane whose required API key is unset come back as a
    structured ``ok=False`` result (fail-fast), not a raise or a silent stall."""
    if preflight_check:
        health = lane_health(request.model, root)
        if not health.ok:
            return DispatchResult(
                ok=False, rc=2, error=f"lane preflight failed: {health.reason}",
                model=request.model,
            )
    try:
        spec = resolve_provider(request.model, root)
    except (ValueError, FileNotFoundError) as exc:
        return DispatchResult(ok=False, rc=2, error=str(exc), model=request.model)
    # Merge the lane's pinned env UNDER any per-request overrides.
    merged_env = {**dict(spec.env), **request.env}
    effective = (
        request
        if merged_env == request.env
        else DispatchRequest(
            model=request.model,
            prompt=request.prompt,
            timeout_s=request.timeout_s,
            max_turns=request.max_turns,
            env=merged_env,
        )
    )
    if request.model == "codex":
        return _dispatch_codex_via_wrapper(effective, spec)
    return dispatch(
        effective,
        spec.command,
        parse_usage=spec.parse_usage,  # type: ignore[arg-type]
        parse_cost=spec.parse_cost,  # type: ignore[arg-type]
        parse_text=spec.parse_text,  # type: ignore[arg-type]
    )


def _read_codex_sidecars(usage_path: str, cost_path: str) -> tuple[TokenUsage, float]:
    """Read cl_codex.sh's sidecars: MO_USAGE_FILE is a TSV ``in<TAB>out`` line;
    MO_COST_FILE is a single float. Missing/garbled → zeros."""
    in_tok = out_tok = 0
    cost = 0.0
    try:
        with open(usage_path, encoding="utf-8") as fh:
            parts = fh.read().strip().split("\t")
            if len(parts) >= 2:
                in_tok, out_tok = int(parts[0] or 0), int(parts[1] or 0)
    except (OSError, ValueError):
        pass
    try:
        with open(cost_path, encoding="utf-8") as fh:
            cost = float(fh.read().strip() or 0.0)
    except (OSError, ValueError):
        pass
    return TokenUsage(input_tokens=in_tok, output_tokens=out_tok), cost


def _dispatch_codex_via_wrapper(
    request: DispatchRequest, spec: ProviderSpec
) -> DispatchResult:
    """codex telemetry lives in sidecar files the wrapper writes (stdout is the
    cleaned text). Point MO_USAGE_FILE/MO_COST_FILE at temp files, dispatch, then
    fold the sidecar usage/cost into the result. MO_USAGE_FILE MUST end in
    ``.tokens`` — cl_codex.sh derives its stream-file path from it."""
    import tempfile

    fd_u, usage_path = tempfile.mkstemp(suffix=".tokens")
    os.close(fd_u)
    fd_c, cost_path = tempfile.mkstemp(suffix=".cost")
    os.close(fd_c)
    try:
        env = {**request.env, "MO_USAGE_FILE": usage_path, "MO_COST_FILE": cost_path}
        req = DispatchRequest(
            model=request.model,
            prompt=request.prompt,
            timeout_s=request.timeout_s,
            max_turns=request.max_turns,
            env=env,
        )
        result = dispatch(req, spec.command)
        if result.ok:
            usage, cost = _read_codex_sidecars(usage_path, cost_path)
            result.usage = usage
            result.cost_usd = cost
        return result
    finally:
        for path in (usage_path, cost_path, f"{usage_path[:-7]}.stream.jsonl"):
            try:
                os.unlink(path)
            except OSError:
                pass


def dispatch_with_command(
    request: DispatchRequest,
    command: Sequence[str],
    *,
    parse_usage: object | None = None,
    parse_cost: object | None = None,
    parse_text: object | None = None,
) -> DispatchResult:
    """Escape hatch for tests / custom commands: dispatch an explicit argv."""
    return dispatch(
        request,
        command,
        parse_usage=parse_usage,  # type: ignore[arg-type]
        parse_cost=parse_cost,  # type: ignore[arg-type]
        parse_text=parse_text,  # type: ignore[arg-type]
    )
