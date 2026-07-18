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
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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


def claude_session_id(stdout: str) -> str:
    """Extract the conversation id from claude's JSON envelope (.session_id).

    `claude --print --output-format json` emits a stable ``session_id`` that
    keys the on-disk transcript under ``~/.claude/projects/<hash>/<id>.jsonl``.
    Capturing it is what makes turn-resume (``--resume <id>``) possible for a
    node that stopped mid-conversation (durable-dag E4). "" when absent."""
    return str(_claude_envelope(stdout).get("session_id") or "")


def apply_resume(command: Sequence[str], session_id: str) -> tuple[str, ...]:
    """Insert ``--resume <session_id>`` into a claude argv so a recovery
    continues the SAME conversation instead of starting fresh (E4, priority-A:
    "resurrect a failed node at turn 9").

    Only rewrites a ``claude`` invocation (EXECUTABLE_MODELS codex/gemini have
    their own session model — node-level resume only, handled elsewhere). The
    flag goes right after ``claude`` so it precedes ``--print``/tool flags,
    matching how the CLI expects a resume target. Idempotent: a command that
    already carries ``--resume`` is returned unchanged.
    """
    if not session_id or not command or command[0] != "claude":
        return tuple(command)
    if "--resume" in command:
        return tuple(command)
    out = [command[0], "--resume", session_id, *command[1:]]
    return tuple(out)


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
    parse_session: object | None = None  # SessionParser | None (E4)
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


def _load_providers_registry(
    root: str | os.PathLike[str] | None = None,
) -> Mapping[str, object]:
    candidates: list[Path] = []
    if override := os.environ.get("MINI_ORK_PROVIDERS"):
        candidates.append(Path(override))
    home = Path(os.environ.get("MINI_ORK_HOME", ".mini-ork"))
    candidates.append(home / "config" / "providers.yaml")
    candidates.append(mini_ork_root(root) / "config" / "providers.yaml")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"invalid providers registry {path}: {exc}") from exc
        if not isinstance(document, Mapping):
            raise ValueError(f"invalid providers registry {path}: root must be a mapping")
        providers = document.get("providers") or {}
        if not isinstance(providers, Mapping):
            raise ValueError(
                f"invalid providers registry {path}: 'providers' must be a mapping"
            )
        return providers
    return {}


def _resolve_from_registry(
    name: str,
    registry: Mapping[str, object],
    root: str | os.PathLike[str] | None = None,
) -> ProviderSpec:
    entry = registry.get(name)
    if not isinstance(entry, Mapping):
        # "unknown lane" (not "unknown model lane") is the load-bearing substring:
        # dispatch_model's preflight surfaces this verbatim and test_dispatch_py +
        # the bash lane_health contract both grep for "unknown lane".
        raise ValueError(f"unknown lane: {name!r}")

    kind = entry.get("kind")
    supported = {
        "anthropic-native",
        "anthropic-compat",
        "openai-compat",
        "executable",
    }
    if kind not in supported:
        raise ValueError(f"provider {name!r} has unsupported kind: {kind!r}")

    raw_extra_env = entry.get("extra_env") or {}
    if not isinstance(raw_extra_env, Mapping):
        raise ValueError(f"provider {name!r} field 'extra_env' must be a mapping")
    extra_env = {str(key): str(value) for key, value in raw_extra_env.items()}
    model_id = str(entry.get("model") or "")

    if kind in {"anthropic-native", "anthropic-compat"}:
        env: dict[str, str] = {}
        if kind == "anthropic-native":
            if api_key := os.environ.get("ANTHROPIC_API_KEY"):
                env["ANTHROPIC_API_KEY"] = api_key
                if model_id:
                    env["ANTHROPIC_MODEL"] = model_id
        else:
            base_url = entry.get("base_url")
            if not isinstance(base_url, str) or not base_url:
                raise ValueError(f"provider {name!r} missing required field 'base_url'")
            api_key_env = entry.get("api_key_env")
            if not isinstance(api_key_env, str) or not api_key_env:
                raise ValueError(f"provider {name!r} missing required field 'api_key_env'")
            env.update(
                {
                    "ANTHROPIC_AUTH_TOKEN": os.environ.get(api_key_env, ""),
                    "ANTHROPIC_BASE_URL": base_url,
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                }
            )
            if model_id:
                env.update(
                    {
                        "ANTHROPIC_MODEL": model_id,
                        "ANTHROPIC_SMALL_FAST_MODEL": model_id,
                        "ANTHROPIC_DEFAULT_OPUS_MODEL": model_id,
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": model_id,
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model_id,
                        "CLAUDE_CODE_SUBAGENT_MODEL": model_id,
                    }
                )
        env.update(extra_env)
        return ProviderSpec(
            model=name,
            command=(
                "claude",
                "--print",
                "--permission-mode",
                "bypassPermissions",
                "--output-format",
                "json",
            ),
            parse_usage=parse_claude_usage,
            parse_cost=claude_cost,
            parse_text=claude_result_text,
            parse_session=claude_session_id,
            env=env,
        )

    if kind == "openai-compat":
        base_url = entry.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError(f"provider {name!r} missing required field 'base_url'")
        api_key_env = entry.get("api_key_env")
        if not isinstance(api_key_env, str) or not api_key_env:
            raise ValueError(f"provider {name!r} missing required field 'api_key_env'")
        wrapper = mini_ork_root(root) / "lib" / "providers" / "cl_codex.sh"
        if not wrapper.is_file():
            raise FileNotFoundError(f"provider wrapper not found: {wrapper}")
        env = {
            "MO_OAI_BASE_URL": base_url,
            "MO_OAI_ENV_KEY": api_key_env,
        }
        if model_id:
            env["MO_OAI_MODEL"] = model_id
        env.update(extra_env)
        return ProviderSpec(
            model=name,
            command=(str(wrapper), "--print", "--output-format", "text"),
            env=env,
        )

    script_value = entry.get("script")
    if not isinstance(script_value, str) or not script_value:
        raise ValueError(f"provider {name!r} missing required field 'script'")
    script = Path(script_value)
    if not script.is_absolute():
        script = mini_ork_root(root) / script
    if not script.is_file():
        raise FileNotFoundError(f"provider script not found: {script}")
    if not os.access(script, os.X_OK):
        raise ValueError(f"provider {name!r} script is not executable: {script}")
    return ProviderSpec(
        model=name,
        command=(str(script), "--print", "--output-format", "text"),
        env=extra_env,
    )


def resolve_provider(
    model: str, root: str | os.PathLike[str] | None = None
) -> ProviderSpec:
    """Resolve a built-in wrapper or registry-defined model lane."""
    wrapper = mini_ork_root(root) / "lib" / "providers" / f"cl_{model}.sh"
    if model not in KNOWN_MODELS:
        return _resolve_from_registry(model, _load_providers_registry(root), root)
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
    # `--permission-mode bypassPermissions` is LOAD-BEARING: without it, `claude
    # --print` runs in the default permission mode and auto-denies every Write/
    # Edit/Bash-redirect in non-interactive mode, so an implementer/worker lane
    # can read but never write files — the port silently produces nothing. The
    # bash path (lib/llm-dispatch.sh:938, lib/lane-helpers.sh:244) always passes
    # it; the Python port dropped it (2026-07-04 migration batch, 3rd killer).
    return ProviderSpec(
        model=model,
        command=("claude", "--print", "--permission-mode", "bypassPermissions",
                 "--output-format", "json"),
        parse_usage=parse_claude_usage,
        parse_cost=claude_cost,
        parse_text=claude_result_text,
        parse_session=claude_session_id,
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
    """Cheap pre-dispatch check for wrapper and registry-defined lanes."""
    if model in KNOWN_MODELS:
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

    try:
        registry = _load_providers_registry(root)
        _resolve_from_registry(model, registry, root)
    except (FileNotFoundError, ValueError) as exc:
        return LaneHealth(False, str(exc))

    entry = registry[model]
    if isinstance(entry, Mapping) and entry.get("kind") in {
        "anthropic-compat",
        "openai-compat",
    }:
        api_key_env = entry["api_key_env"]
        if not os.environ.get(str(api_key_env)):
            return LaneHealth(
                False,
                f"{model}: ${api_key_env} is not set — lane would die silently",
            )
    return LaneHealth(True, "ok")


def preflight(
    models: Sequence[str], root: str | os.PathLike[str] | None = None
) -> dict[str, LaneHealth]:
    """Health-check several lanes at once (e.g. every lane a recipe will use).
    Returns {model: LaneHealth}; callers abort the run if any are unhealthy."""
    return {m: lane_health(m, root) for m in dict.fromkeys(models)}


def resolve_target_cwd(
    request: DispatchRequest, *, env: Mapping[str, str] | None = None
) -> str:
    """The directory the provider runs in. Precedence: request.cwd →
    $MO_TARGET_CWD → the current process cwd. Returns an absolute path so the
    dispatch is never at the mercy of an inherited, drifted cwd."""
    e = os.environ if env is None else env
    cwd = request.cwd or e.get("MO_TARGET_CWD") or os.getcwd()
    return os.path.abspath(cwd)


def cwd_guard(
    cwd: str,
    root: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> LaneHealth:
    """Refuse a dispatch whose working directory is INSIDE the mini-ork framework
    tree. That is the cwd-confusion (a target-repo lane landing in mini-ork
    itself) under which the provider's own git operations — e.g. codex's
    `refs/codex/*` resets — corrupt the framework repo. A genuine mini-ork
    self-edit opts in with MO_ALLOW_FRAMEWORK_CWD=1."""
    e = os.environ if env is None else env
    if e.get("MO_ALLOW_FRAMEWORK_CWD") == "1":
        return LaneHealth(True, "ok")
    framework = mini_ork_root(root)
    target = Path(cwd).resolve()
    try:
        target.relative_to(framework)
    except ValueError:
        return LaneHealth(True, "ok")  # outside the framework tree — fine
    return LaneHealth(
        False,
        f"dispatch cwd {target} is inside the mini-ork framework tree {framework} "
        "— likely cwd-confusion (a target-repo lane landing in mini-ork). Set "
        "MO_TARGET_CWD to the target repo, or MO_ALLOW_FRAMEWORK_CWD=1 for a "
        "genuine mini-ork self-edit.",
    )


def dispatch_model(
    request: DispatchRequest,
    root: str | os.PathLike[str] | None = None,
    *,
    preflight_check: bool = True,
) -> DispatchResult:
    """Resolve ``request.model`` to a provider and dispatch it. Unknown lane /
    missing wrapper / unset API key / a framework-tree cwd come back as a
    structured ``ok=False`` result (fail-fast), not a raise, stall, or repo
    corruption."""
    # Always pin an explicit, absolute cwd so the provider can't inherit a
    # drifted one. The guard (below) is what refuses the dangerous case.
    target_cwd = resolve_target_cwd(request)
    if preflight_check:
        health = lane_health(request.model, root)
        if not health.ok:
            return DispatchResult(
                ok=False, rc=2, error=f"lane preflight failed: {health.reason}",
                model=request.model,
            )
        guard = cwd_guard(target_cwd, root)
        if not guard.ok:
            return DispatchResult(
                ok=False, rc=2, error=f"cwd guard failed: {guard.reason}",
                model=request.model,
            )
    try:
        spec = resolve_provider(request.model, root)
    except (ValueError, FileNotFoundError) as exc:
        return DispatchResult(ok=False, rc=2, error=str(exc), model=request.model)
    # Node-scoped tool grants (kickoff/tool-grant-hermetic-dispatch.md):
    # resolve the active node's capability grant from MO_NODE_ID/MO_NODE_TYPE
    # + workflow.yaml, then inject --allowedTools + --strict-mcp-config
    # (+ --mcp-config when MCP grants are present and MINI_ORK_RUN_DIR is set)
    # into the claude argv. EXECUTABLE_MODELS (codex/gemini) are skipped —
    # their CLIs don't accept --allowedTools and bash never passes it to them.
    # MO_TOOL_GRANTS_DISABLED=1 opt-out for local debugging, matching the bash
    # dispatch's escape hatch at lib/llm-dispatch.sh:981.
    if request.model not in EXECUTABLE_MODELS:
        effective_env = {**os.environ, **request.env}
        command = spec.command
        if effective_env.get("MO_TOOL_GRANTS_DISABLED", "0") != "1":
            run_dir = effective_env.get("MINI_ORK_RUN_DIR", "") or None
            command = apply_tool_grants(command, env=effective_env, run_dir=run_dir)
        # E4 turn-resume: the recovery path exports MO_RESUME_SESSION_ID for a
        # node with a persisted session. Insert `--resume <id>` so the claude
        # lane continues the interrupted conversation instead of starting over.
        # Claude-family only — codex/gemini (EXECUTABLE_MODELS) are excluded
        # above and fall back to node-level resume.
        resume_id = effective_env.get("MO_RESUME_SESSION_ID", "").strip()
        if resume_id:
            command = apply_resume(command, resume_id)
        if command != spec.command:
            spec = ProviderSpec(
                model=spec.model,
                command=command,
                parse_usage=spec.parse_usage,
                parse_cost=spec.parse_cost,
                parse_text=spec.parse_text,
                parse_session=spec.parse_session,
                env=spec.env,
            )
    # Merge the lane's pinned env UNDER any per-request overrides; pin the cwd.
    merged_env = {**dict(spec.env), **request.env}
    effective = DispatchRequest(
        model=request.model,
        prompt=request.prompt,
        timeout_s=request.timeout_s,
        max_turns=request.max_turns,
        env=merged_env,
        cwd=target_cwd,
    )
    if request.model == "codex":
        return _dispatch_codex_via_wrapper(effective, spec)
    result = dispatch(
        effective,
        spec.command,
        parse_usage=spec.parse_usage,  # type: ignore[arg-type]
        parse_cost=spec.parse_cost,  # type: ignore[arg-type]
        parse_text=spec.parse_text,  # type: ignore[arg-type]
        parse_session=spec.parse_session,  # type: ignore[arg-type]
    )
    _stash_session_id(result.session_id, merged_env)
    return result


def _stash_session_id(session_id: str, env: Mapping[str, str]) -> None:
    """E4: write the captured claude session id to a per-node sidecar
    (``<run_dir>/.sessions/<node_id>.session``) so the checkpoint writer can
    record it as ``node_attempts.provider_session_id`` without changing the
    dispatch_fn return contract. Best-effort; keyed by MINI_ORK_RUN_DIR +
    MO_NODE_ID which the execute loop sets before every node dispatch."""
    if not session_id:
        return
    run_dir = env.get("MINI_ORK_RUN_DIR") or os.environ.get("MINI_ORK_RUN_DIR") or ""
    node_id = env.get("MO_NODE_ID") or os.environ.get("MO_NODE_ID") or ""
    if not run_dir or not node_id:
        return
    try:
        d = Path(run_dir) / ".sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{node_id}.session").write_text(session_id)
    except OSError:
        pass


# ─── Node-scoped tool grants (kickoff/tool-grant-hermetic-dispatch.md) ───────
# Faithful Python port of the bash resolver in lib/llm-dispatch.sh
# (_mo_resolve_node_tools / _mo_default_tools_for_type / _mo_build_allowed_tools_arg /
# _mo_write_node_mcp_config). Resolves a workflow.yaml `tools:` block per active
# node and injects hermetic --allowedTools + --strict-mcp-config + --mcp-config
# argv onto the claude invocation. Without this every dispatch silently lost
# the previous attempt's consumer-only wiring and gave undeclared implementer
# nodes Read,Bash — no Write/Edit — which broke 12+ recipes' worker lanes.

# Type-aware fail-closed defaults. "Fail closed" means "no MCP and no ambient
# leakage", NOT "no ability to do the job" — an undeclared implementer must
# still be able to write files or every recipe breaks.
def _mo_default_tools_for_type(node_type: str) -> str:
    if node_type == "implementer":
        return "Read,Write,Edit,Bash|"
    return "Read,Bash|"


def _resolve_node_tools_from_yaml(yaml_path: str, node_id: str) -> str:
    """Producer: parse a workflow.yaml, find the node by name (with -/_ variants),
    extract its `tools:` block. Prints "native_csv|mcp_csv" to stdout. Empty
    string when the node isn't declared — caller applies the type-aware default."""
    p = Path(yaml_path)
    if not p.is_file():
        return ""

    def _try_yaml() -> str | None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            return None
        try:
            with open(p, encoding="utf-8") as fh:
                d = yaml.safe_load(fh) or {}
        except (OSError, ValueError, TypeError):
            return ""
        nodes = d.get("nodes") or []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            nm = str(n.get("name") or "")
            if nm == node_id or nm.replace("-", "_") == node_id.replace("-", "_"):
                tools = n.get("tools") or {}
                if not isinstance(tools, dict):
                    return ""
                native = tools.get("native") or []
                mcp = tools.get("mcp") or []
                if not isinstance(native, list):
                    native = list(native) if native else []
                if not isinstance(mcp, list):
                    mcp = list(mcp) if mcp else []
                return "%s|%s" % (
                    ",".join(str(x) for x in native),
                    ",".join(str(x) for x in mcp),
                )
        return ""

    # Regex fallback: robust to either yq or yaml format. Handles the multi-doc
    # case by scanning all `name:` lines and resetting the active block.
    def _regex_fallback() -> str:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return ""
        norm = node_id.replace("-", "_")
        blocks = re.split(r"\n(?=  - name: |\n  - name: )", text)
        for blk in blocks:
            m = re.search(r"^\s*-?\s*name:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?", blk, re.M)
            if not m:
                continue
            if m.group(1) != node_id and m.group(1).replace("-", "_") != norm:
                continue
            tools_m = re.search(r"^\s*tools:\s*\n((?:\s+\S.*\n)+)", blk, re.M)
            if not tools_m:
                return ""
            body = tools_m.group(1)
            nat_m = re.search(r"native:\s*\[([^\]]*)\]", body)
            mcp_m = re.search(r"mcp:\s*\[([^\]]*)\]", body)

            def _split(s: str | None) -> str:
                if not s:
                    return ""
                return ",".join(
                    t.strip().strip("'\"") for t in s.split(",") if t.strip()
                )

            return "%s|%s" % (
                _split(nat_m.group(1) if nat_m else ""),
                _split(mcp_m.group(1) if mcp_m else ""),
            )
        return ""

    result = _try_yaml()
    if result is None:
        result = _regex_fallback()
    return result


def _resolve_node_tools(env: Mapping[str, str]) -> str:
    """Env-aware entry: MO_RESOLVED_NODE_TOOLS override → workflow.yaml lookup →
    type-aware default. Mirrors _mo_resolve_node_tools in lib/llm-dispatch.sh."""
    override = env.get("MO_RESOLVED_NODE_TOOLS", "")
    if override:
        return override
    yaml_path = env.get("MO_WORKFLOW_YAML", "")
    if not yaml_path or not Path(yaml_path).is_file():
        run_dir = env.get("MINI_ORK_RUN_DIR", "")
        if run_dir:
            candidate = Path(run_dir) / "workflow.yaml"
            if candidate.is_file():
                yaml_path = str(candidate)
    node_id = env.get("MO_NODE_ID", "")
    node_type = env.get("MO_NODE_TYPE", "")
    resolved = ""
    if yaml_path and node_id:
        resolved = _resolve_node_tools_from_yaml(yaml_path, node_id)
    # The yaml resolver returns the bare "|" for a node with no tools: block —
    # that's a non-empty string, so a plain truthiness check would skip the
    # default and resolve every undeclared node to NO tools. Treat "|" as
    # unresolved. Same footgun fixed in bash at llm-dispatch.sh:873-877.
    if not resolved or resolved == "|":
        resolved = _mo_default_tools_for_type(node_type)
        if node_id:
            sys.stderr.write(
                f"[tool-grants] node={node_id} type={node_type or 'unknown'} "
                "undeclared; applying type-aware default\n"
            )
    return resolved


def _build_allowed_tools_arg(native_csv: str, mcp_csv: str) -> str:
    """Compose the comma-separated value passed to --allowedTools. Native tools
    pass through verbatim; MCP grants render as mcp__<server>."""
    parts: list[str] = []
    for csv in (native_csv or "", mcp_csv or ""):
        for raw in csv.split(","):
            tok = raw.strip()
            if not tok:
                continue
            if csv is mcp_csv:
                if not tok.startswith("mcp__"):
                    tok = f"mcp__{tok}"
            parts.append(tok)
    return ",".join(parts)


def _write_node_mcp_config(
    mcp_csv: str, run_dir: str, *, env: Mapping[str, str] | None = None
) -> str | None:
    """Write a node-scoped .mcp-config.json containing only the granted MCP
    servers. Sources the operator's MCP server definitions from
    ${MINI_ORK_HOME}/config/mcp_servers.json (operator home) or
    ${MINI_ORK_ROOT}/.claude/mcp_servers.json (repo fallback), filters to the
    granted subset, writes to ${run_dir}/.mcp-config.json. Granted servers not
    in the operator config get a no-op stub so --strict-mcp-config keeps the
    dispatch hermetic."""
    if not run_dir:
        return None
    granted = [s.strip() for s in (mcp_csv or "").split(",") if s.strip()]
    if not granted:
        return None
    e = env if env is not None else os.environ
    home = e.get("MINI_ORK_HOME", "") or ""
    root = e.get("MINI_ORK_ROOT", "") or ""
    sources: list[str] = []
    if home:
        sources.append(os.path.join(home, "config", "mcp_servers.json"))
    if root:
        sources.append(os.path.join(root, ".claude", "mcp_servers.json"))
        sources.append(os.path.join(root, "mcp", "steering.json"))
    operator: dict[str, object] = {}
    for src in sources:
        if not src or not os.path.isfile(src):
            continue
        try:
            with open(src, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(d, dict) and isinstance(d.get("mcpServers"), dict):
            operator = d["mcpServers"]  # type: ignore[assignment]
            break
        if isinstance(d, dict):
            operator = d
            break
    scoped: dict[str, object] = {}
    for g in granted:
        candidate = operator.get(g) if isinstance(operator, dict) else None
        if isinstance(candidate, dict):
            scoped[g] = candidate
        else:
            scoped[g] = {"command": "echo", "args": [f"no-op: {g} not configured"]}
    out_dir = Path(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ".mcp-config.json"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": scoped}, fh, indent=2)
    except OSError:
        return None
    return str(out_path)


def apply_tool_grants(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    run_dir: str | None = None,
) -> tuple[str, ...]:
    """Insert --allowedTools, --strict-mcp-config (always), and --mcp-config
    (when MCP grants present + run_dir given) into a claude argv. Inserts at
    the position bash uses (after --permission-mode, before --output-format)
    so the resulting argv is byte-for-byte equivalent to the bash path's.
    Returns the original command unchanged when the resolver returns an empty
    grant — an undeclared implementer MUST still default to
    Read,Write,Edit,Bash so implementer recipes don't silently go read-only."""
    # Defense-in-depth: --allowedTools/--strict-mcp-config are claude CLI flags.
    # Today every caller path passes a claude command (kimi/minimax/glm route
    # through the claude binary; codex/gemini are excluded upstream), but guard
    # here so a future non-claude lane can never have these injected into its argv.
    if not command or command[0] != "claude":
        return tuple(command)
    resolved = _resolve_node_tools(env)
    if not resolved or "|" not in resolved:
        return tuple(command)
    native_csv, mcp_csv = resolved.split("|", 1)
    allowed = _build_allowed_tools_arg(native_csv, mcp_csv)
    if not allowed:
        return tuple(command)
    tool_flags = ["--allowedTools", allowed, "--strict-mcp-config"]
    rd = run_dir or env.get("MINI_ORK_RUN_DIR", "")
    if rd and mcp_csv.strip():
        cfg_path = _write_node_mcp_config(mcp_csv, rd, env=env)
        if cfg_path:
            tool_flags += ["--mcp-config", cfg_path]
    # Insertion point: right before --output-format (or end), matching bash's
    # argv layout (--permission-mode <then tool-flags> --output-format). When
    # --output-format isn't present, append (safe fallback for command shapes
    # this codebase hasn't shipped yet).
    new = list(command)
    insert_idx = len(new)
    for i, arg in enumerate(new):
        if arg == "--output-format":
            insert_idx = i
            break
    new[insert_idx:insert_idx] = tool_flags
    return tuple(new)


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
            cwd=request.cwd,  # preserve the guarded target cwd through the codex path
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


def dispatch_with_fallback(
    request: DispatchRequest,
    lanes: Sequence[str],
    root: str | os.PathLike[str] | None = None,
    *,
    per_attempt_timeout_s: float | None = None,
) -> DispatchResult:
    """Dispatch ``request`` trying each lane in ``lanes`` in order, returning the
    first result that succeeds with non-empty output. A lane that fails preflight
    (dead/unset key), times out (a HUNG lane — the recurring stall), returns a
    non-zero rc, or emits empty text is abandoned and the next lane is tried.

    This is the fix for the single biggest reliability failure: one flaky/hung
    lane (codex flaky in some envs, glm/kimi/minimax gateways hang in others)
    blocking a whole run for the full 25-min timeout with no recovery. With a
    fallback chain, a hung lane costs one attempt-timeout and the run continues
    on a healthy lane instead of stalling delivery.

    Returns the last failed result if every lane fails (so the caller still sees
    a faithful rc/error, never a silent hang).
    """
    import sys

    last: DispatchResult | None = None
    tried: list[str] = []
    for i, lane in enumerate(lanes):
        req = DispatchRequest(
            model=lane,
            prompt=request.prompt,
            timeout_s=per_attempt_timeout_s or request.timeout_s,
            max_turns=request.max_turns,
            env=dict(request.env),
            cwd=request.cwd,
        )
        result = dispatch_model(req, root)
        tried.append(lane)
        if result.ok and (result.text or "").strip():
            if i > 0:
                sys.stderr.write(
                    f"[dispatch] lane fallback: {'/'.join(tried[:-1])} failed → "
                    f"served by {lane}\n"
                )
            return result
        sys.stderr.write(
            f"[dispatch] lane {lane} failed (rc={result.rc} "
            f"{(result.error or 'empty output')[:80]}); trying next\n"
        )
        last = result
    if last is None:
        return DispatchResult(ok=False, rc=2, error="no lanes provided",
                              model=request.model)
    return last


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
