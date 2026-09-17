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
    # Isolation selector (SE-3 SC3): WHICH Workspace the harness CLI itself is
    # spawned into. "host" = today's in-process Popen on this machine (the
    # default → zero regression). Any other value ("docker"/"microvm"/"local")
    # routes the spawn through the matching Workspace.spawn backend, so a run can
    # isolate the coding-agent CLI, not just its tool-exec. Set from
    # MO_SANDBOX_SCOPE=agent at dispatch_model; the resolved backend name comes
    # from MO_SANDBOX_BACKEND. Frozen with a default so every existing caller
    # keeps host behavior untouched.
    workspace: str = "host"


# SE-3 Phase B2: a dispatch refused because the resolved engine cannot honor
# the node's capability envelope (e.g. a node that declares MCP servers routed
# to an engine with no MCP translation). Distinct from SHAPE_REJECT_RC (65) so
# callers can tell "lane can't carry the capability" from "lane emitted the
# wrong artifact shape".
ENVELOPE_REJECT_RC = 66

# Node-boundary env vars carrying the envelope (published by dispatch_node,
# masked to None when the node declares nothing — the MO_RESUME_SESSION_ID
# stale-leak discipline).
ENV_MCP_SERVERS = "MO_MCP_SERVERS"
ENV_SKILLS = "MO_SKILLS"
ENV_AGENT_DOC = "MO_AGENT_DOC"


@dataclass(frozen=True)
class CapabilityEnvelope:
    """Per-node harness capability declarations (SE-3 Phase B2).

    What a workflow node asks its harness for beyond the prompt: MCP servers,
    named skills, and an agent-doc. Engines declare per-axis support via
    ``Capabilities``; a node declaring an axis the resolved engine cannot
    translate is rejected loudly (rc=ENVELOPE_REJECT_RC) instead of silently
    dispatching with the capability dropped — the lane-binding failure class
    where a recipe works on one lane and quietly degrades on another.

    Values are server/skill *names* resolved by the receiving side: the claude
    engine materializes MCP names against the operator's mcp_servers.json;
    a UHP server translates them into its target harness's native config.
    """

    mcp_servers: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    agent_doc: str = ""

    def is_empty(self) -> bool:
        return not (self.mcp_servers or self.skills or self.agent_doc)

    def as_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.mcp_servers:
            out[ENV_MCP_SERVERS] = ",".join(self.mcp_servers)
        if self.skills:
            out[ENV_SKILLS] = ",".join(self.skills)
        if self.agent_doc:
            out[ENV_AGENT_DOC] = self.agent_doc
        return out

    def unsupported_axes(self, capabilities) -> tuple[str, ...]:
        """The envelope axes ``capabilities`` (a ``Capabilities`` instance)
        does not accept, as axis names for the rejection message. An axis with
        nothing declared is never unsupported."""
        checks = (
            ("mcp_servers", not self.mcp_servers or capabilities.mcp_servers),
            ("skills", not self.skills or capabilities.skills),
            ("agent_doc", not self.agent_doc or capabilities.agent_doc),
        )
        return tuple(name for name, ok in checks if not ok)


def envelope_from_env(read) -> "CapabilityEnvelope":
    """Build an envelope by reading the carrier vars through ``read(key)``
    (pass ``context_env`` at dispatch seams, ``env.get`` in tests/transports).
    Missing/blank vars contribute nothing."""
    def _csv(value: str) -> tuple[str, ...]:
        return tuple(tok.strip() for tok in (value or "").split(",") if tok.strip())

    return CapabilityEnvelope(
        mcp_servers=_csv(read(ENV_MCP_SERVERS)),
        skills=_csv(read(ENV_SKILLS)),
        agent_doc=(read(ENV_AGENT_DOC) or "").strip(),
    )


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
    # Provider conversation id (claude --output-format json emits `session_id`).
    # Captured so a failed node can be resumed at its interrupted turn via
    # `claude --resume <session_id>` (durable-dag E4). "" when the provider
    # does not surface one (codex/gemini use their own session model).
    session_id: str = ""
