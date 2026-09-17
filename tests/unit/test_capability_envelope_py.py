"""SE-3 Phase B2 — the per-node capability envelope.

A workflow node declares harness-level capabilities (mcp_servers / skills /
agent_doc); the declarations ride the node env bus (MO_MCP_SERVERS /
MO_SKILLS / MO_AGENT_DOC); dispatch_model checks them against the resolved
engine's Capabilities and rejects loudly (rc=ENVELOPE_REJECT_RC) when an
engine cannot translate a declared axis — the structural fix for lane-bound
recipes (framework_edit lane-binding bug class), where routing a node to a
different lane silently dropped its MCP/skill setup.
"""

import textwrap

from mini_ork.context import run_context_scope
from mini_ork.dispatch.models import (
    ENVELOPE_REJECT_RC,
    CapabilityEnvelope,
    DispatchRequest,
    envelope_from_env,
)
from mini_ork.dispatch.providers import (
    ENGINES,
    Capabilities,
    apply_tool_grants,
    dispatch_model,
)
from mini_ork.workflow.compiler import compile_workflow


# ── Envelope helpers ─────────────────────────────────────────────────────────


def test_envelope_from_env_parses_csv_and_blanks():
    env = envelope_from_env(
        lambda k: {"MO_MCP_SERVERS": " a , b ", "MO_SKILLS": "", "MO_AGENT_DOC": " d.md "}.get(k, "")
    )
    assert env.mcp_servers == ("a", "b")
    assert env.skills == ()
    assert env.agent_doc == "d.md"
    assert not env.is_empty()
    assert env.as_env() == {"MO_MCP_SERVERS": "a,b", "MO_AGENT_DOC": "d.md"}


def test_empty_envelope_is_a_no_op():
    env = envelope_from_env(lambda k: "")
    assert env.is_empty()
    assert env.as_env() == {}
    # Nothing declared → never unsupported, on any engine.
    assert env.unsupported_axes(Capabilities()) == ()


def test_unsupported_axes_reports_only_declared_and_refused_axes():
    env = CapabilityEnvelope(mcp_servers=("s",), skills=("k",), agent_doc="d")
    assert env.unsupported_axes(ENGINES["uhp"].capabilities()) == ()
    assert env.unsupported_axes(ENGINES["codex"].capabilities()) == (
        "mcp_servers",
        "skills",
        "agent_doc",
    )
    # claude translates mcp_servers (--mcp-config) but not skills/agent_doc.
    assert env.unsupported_axes(ENGINES["claude"].capabilities()) == ("skills", "agent_doc")


# ── Workflow contract ────────────────────────────────────────────────────────


def test_workflow_node_carries_envelope_declarations(tmp_path):
    wf = tmp_path / "workflow.yaml"
    wf.write_text(
        textwrap.dedent(
            """
            task_class: generic
            nodes:
              - name: n1
                type: researcher
                mcp_servers: [websearch, github]
                skills: doc-writer
                agent_doc: prompts/AGENTS.md
            edges: []
            """
        ),
        encoding="utf-8",
    )
    node = compile_workflow(str(wf)).nodes["n1"]
    assert node.mcp_servers == ("websearch", "github")
    assert node.skills == ("doc-writer",)
    assert node.agent_doc == "prompts/AGENTS.md"


def test_workflow_node_without_envelope_compiles_to_empty(tmp_path):
    wf = tmp_path / "workflow.yaml"
    wf.write_text(
        textwrap.dedent(
            """
            task_class: generic
            nodes:
              - name: n1
                type: researcher
            edges: []
            """
        ),
        encoding="utf-8",
    )
    node = compile_workflow(str(wf)).nodes["n1"]
    assert node.mcp_servers == ()
    assert node.skills == ()
    assert node.agent_doc == ""


# ── Claude translator: envelope servers merge into the grants path ───────────


def test_apply_tool_grants_unions_envelope_servers(tmp_path):
    cmd = apply_tool_grants(
        ("claude", "--print", "--output-format", "text"),
        env={"MO_MCP_SERVERS": "websearch"},
        run_dir=str(tmp_path),
    )
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__websearch" in allowed
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" in cmd


def test_apply_tool_grants_envelope_only_node_still_gets_grants(tmp_path):
    """A node with no tools: block but a declared envelope must still produce
    the MCP flags (the resolver's type default only covers native tools)."""
    cmd = apply_tool_grants(
        ("claude", "--print", "--output-format", "text"),
        env={"MO_MCP_SERVERS": "websearch"},
        run_dir=str(tmp_path),
    )
    assert "--mcp-config" in cmd


def test_apply_tool_grants_no_envelope_adds_no_mcp_grants(tmp_path):
    """Ratchet: with MO_MCP_SERVERS unset nothing envelope-driven appears —
    the resolver's type default may still grant native tools, but never an
    mcp__ grant or a --mcp-config flag."""
    cmd = apply_tool_grants(
        ("claude", "--print", "--output-format", "text"),
        env={},
        run_dir=str(tmp_path),
    )
    if "--allowedTools" in cmd:
        allowed = cmd[cmd.index("--allowedTools") + 1]
        assert "mcp__" not in allowed
    assert "--mcp-config" not in cmd


# ── dispatch_model: the structural lane-binding check ────────────────────────


def test_dispatch_model_rejects_envelope_on_unsupported_engine(monkeypatch):
    from mini_ork.dispatch import providers

    spec = providers.ProviderSpec(model="codex", command=("true",))
    monkeypatch.setattr(providers, "resolve_provider", lambda *a, **k: spec)
    request = DispatchRequest(model="codex", prompt="hi")
    with run_context_scope({"MO_MCP_SERVERS": "websearch", "MO_SKILLS": "doc-writer"}):
        result = dispatch_model(request, None, preflight_check=False)
    assert not result.ok
    assert result.rc == ENVELOPE_REJECT_RC
    assert "mcp_servers" in result.error
    assert "skills" in result.error


def test_dispatch_model_rejects_envelope_for_uhp_wire_acceptance(monkeypatch):
    """kind=uhp specs pick the UhpEngine: every axis accepted, so the same
    envelope that codex rejected proceeds to the backend (and fails at the
    transport, not the capability check)."""
    from mini_ork.dispatch import providers

    spec = providers.ProviderSpec(
        model="uhp_test", command=("false",), kind="uhp"
    )
    monkeypatch.setattr(providers, "resolve_provider", lambda *a, **k: spec)
    request = DispatchRequest(model="uhp_test", prompt="hi")
    with run_context_scope({"MO_MCP_SERVERS": "websearch", "MO_AGENT_DOC": "d.md"}):
        result = dispatch_model(request, None, preflight_check=False)
    assert result.rc != ENVELOPE_REJECT_RC


def test_dispatch_model_without_envelope_is_unchanged(monkeypatch):
    """Ratchet: no envelope published → no capability check fires; the
    dispatch behaves exactly as before B2 (fails here only at the stub
    backend, with its own rc)."""
    from mini_ork.dispatch import providers

    spec = providers.ProviderSpec(model="codex", command=("false",))
    monkeypatch.setattr(providers, "resolve_provider", lambda *a, **k: spec)
    request = DispatchRequest(model="codex", prompt="hi")
    result = dispatch_model(request, None, preflight_check=False)
    assert result.rc != ENVELOPE_REJECT_RC


# ── Fallback-chain interaction ───────────────────────────────────────────────


def test_fallback_skips_envelope_rejected_lane(monkeypatch):
    """The lane-binding fix end to end: dispatch_with_fallback treats the
    rc=ENVELOPE_REJECT_RC lane as failed and serves the node from a lane that
    CAN honor the envelope (uhp), instead of silently dropping the capability."""
    from mini_ork.dispatch import providers
    from mini_ork.dispatch.providers import dispatch_with_fallback

    specs = {
        "codex": providers.ProviderSpec(model="codex", command=("true",)),
        "uhp_lane": providers.ProviderSpec(model="uhp_lane", command=("true",), kind="uhp"),
    }
    monkeypatch.setattr(
        providers, "resolve_provider", lambda model, *a, **k: specs[model]
    )

    class _Healthy:
        ok = True
        reason = "ok"

    monkeypatch.setattr(providers, "lane_health", lambda *a, **k: _Healthy())
    served: list[str] = []

    def fake_backend(request, spec):
        served.append(request.model)
        from mini_ork.dispatch.models import DispatchResult

        return DispatchResult(ok=True, rc=0, text="done", model=request.model)

    monkeypatch.setattr(
        providers, "MODEL_DISPATCH_BACKENDS", {**providers.MODEL_DISPATCH_BACKENDS,
                                               "codex": fake_backend, "uhp_lane": fake_backend}
    )
    request = DispatchRequest(model="codex", prompt="hi", cwd="/tmp")
    with run_context_scope({"MO_MCP_SERVERS": "websearch"}):
        result = dispatch_with_fallback(request, ["codex", "uhp_lane"], None)
    assert result.ok
    assert served == ["uhp_lane"]
