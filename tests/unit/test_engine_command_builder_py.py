"""SE-3 Phase A.3 — per-engine command-builder hook.

dispatch_model no longer special-cases `if model not in EXECUTABLE_MODELS`
inline; it asks engine_of(model) and delegates to that engine's registered
command builder. The load-bearing invariant these tests pin: the claude engine
rewrites argv (tool grants + E4 resume) while an executable engine (codex) has
NO builder registered, so claude-only flags cannot leak into a CLI that rejects
them — no negative special-case for a future maintainer to remember.
"""

from __future__ import annotations

from mini_ork.dispatch.models import DispatchRequest
from mini_ork.dispatch.providers import (
    ENGINE_COMMAND_BUILDERS,
    ENGINES,
    EXECUTABLE_MODELS,
    _claude_command_builder,
    engine_of,
    register_engine_command_builder,
)


def _req(model: str) -> DispatchRequest:
    return DispatchRequest(model=model, prompt="hi")


# ── engine_of ───────────────────────────────────────────────────────────────


def test_engine_of_claude_family():
    # Every non-executable lane shares the claude engine.
    for model in ("opus", "sonnet", "glm", "kimi", "minimax", "deepseek"):
        assert engine_of(model) == "claude", model


def test_engine_of_executable_is_the_model_itself():
    for model in EXECUTABLE_MODELS:
        assert engine_of(model) == model


def test_engine_of_unknown_defaults_to_claude():
    # An unrecognised lane still resolves to a real engine (fail toward claude,
    # the argv-rewriting path), never to None.
    assert engine_of("some-future-lane") == "claude"


# ── the ratchet: executable engines have no claude builder ───────────────────


def test_executable_engine_has_no_command_builder():
    # THE invariant. codex's engine is not in the registry, so dispatch_model's
    # `ENGINE_COMMAND_BUILDERS.get(engine_of(model))` returns None and the argv
    # is dispatched untouched — --allowedTools/--resume can never be injected.
    for model in EXECUTABLE_MODELS:
        assert ENGINE_COMMAND_BUILDERS.get(engine_of(model)) is None, model


def test_claude_engine_has_a_command_builder():
    assert ENGINE_COMMAND_BUILDERS.get("claude") is _claude_command_builder


# ── _claude_command_builder behaviour ────────────────────────────────────────


def test_builder_injects_resume_when_session_id_present():
    out = _claude_command_builder(
        ("claude", "-p", "hi"),
        request=_req("opus"),
        env={"MO_RESUME_SESSION_ID": "sess-9", "MO_TOOL_GRANTS_DISABLED": "1"},
    )
    assert out == ("claude", "--resume", "sess-9", "-p", "hi")


def test_builder_is_noop_when_grants_disabled_and_no_resume():
    cmd = ("claude", "-p", "hi")
    out = _claude_command_builder(
        cmd, request=_req("glm"), env={"MO_TOOL_GRANTS_DISABLED": "1"}
    )
    assert out == cmd


def test_builder_leaves_non_claude_argv_untouched():
    # Defence in depth: even if a caller wrongly routed a codex argv through the
    # claude builder, apply_tool_grants/apply_resume guard on command[0].
    cmd = ("codex", "exec", "--json")
    out = _claude_command_builder(
        cmd,
        request=_req("codex"),
        env={"MO_RESUME_SESSION_ID": "sess-1"},
    )
    assert out == cmd


# ── registry is open for extension (OCP) ─────────────────────────────────────


def test_register_engine_command_builder_roundtrips():
    sentinel = ("was", "here")

    def _fake(command, *, request, env):
        return sentinel

    try:
        register_engine_command_builder("fake-engine", _fake)
        assert ENGINE_COMMAND_BUILDERS["fake-engine"] is _fake
        assert (
            ENGINE_COMMAND_BUILDERS["fake-engine"](
                ("x",), request=_req("x"), env={}
            )
            == sentinel
        )
    finally:
        ENGINE_COMMAND_BUILDERS.pop("fake-engine", None)
        ENGINES.pop("fake-engine", None)
