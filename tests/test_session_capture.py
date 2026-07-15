"""E4: capture the claude session id + insert --resume on recovery.

Covers the acceptance:
  * session_id is parsed from claude's JSON envelope and surfaced on the result
  * a node being recovered (MO_RESUME_SESSION_ID set) has `--resume <id>`
    inserted into the claude argv (verified against a spy dispatch)
  * codex/gemini lanes never get --resume (their own session model)
  * core.dispatch captures the session id end-to-end from a stub claude
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.dispatch import providers as P  # noqa: E402
from mini_ork.dispatch import core as C  # noqa: E402
from mini_ork.dispatch.models import DispatchRequest, DispatchResult  # noqa: E402


# ── session id parsing ─────────────────────────────────────────────────────
def test_session_id_parsed_from_envelope():
    env = json.dumps({"result": "hi", "session_id": "sess-abc123", "usage": {}})
    assert P.claude_session_id(env) == "sess-abc123"


def test_session_id_empty_when_absent():
    assert P.claude_session_id('{"result":"hi"}') == ""
    assert P.claude_session_id("not json") == ""


# ── apply_resume: claude-only, positioned, idempotent ──────────────────────
def test_resume_turn_flag_inserted_for_claude():
    cmd = ("claude", "--print", "--output-format", "json")
    out = P.apply_resume(cmd, "sess-1")
    assert out[:3] == ("claude", "--resume", "sess-1")
    assert "--print" in out


def test_resume_turn_noop_for_non_claude_lane():
    cmd = ("codex-wrapper.sh", "--print")     # EXECUTABLE_MODELS lane
    assert P.apply_resume(cmd, "sess-1") == cmd


def test_resume_turn_idempotent():
    cmd = ("claude", "--resume", "sess-1", "--print")
    assert P.apply_resume(cmd, "sess-1") == cmd


def test_resume_turn_noop_without_session():
    cmd = ("claude", "--print")
    assert P.apply_resume(cmd, "") == cmd


# ── dispatch_model inserts --resume when MO_RESUME_SESSION_ID is set ────────
def test_dispatch_model_adds_resume_turn_flag(monkeypatch):
    seen = {}

    def spy_dispatch(request, command, **kw):
        seen["command"] = tuple(command)
        return DispatchResult(ok=True, rc=0, text="done", model=request.model, session_id="sess-new")

    def fake_resolve(model, root=None):
        return P.ProviderSpec(
            model=model,
            command=("claude", "--print", "--permission-mode", "bypassPermissions",
                     "--output-format", "json"),
            parse_session=P.claude_session_id,
        )

    monkeypatch.setattr(P, "dispatch", spy_dispatch)
    monkeypatch.setattr(P, "resolve_provider", fake_resolve)
    monkeypatch.setenv("MO_TOOL_GRANTS_DISABLED", "1")
    monkeypatch.setenv("MO_RESUME_SESSION_ID", "sess-resume-9")
    monkeypatch.setenv("MO_TARGET_CWD", os.getcwd())

    res = P.dispatch_model(
        DispatchRequest(model="minimax", prompt="hi", cwd=os.getcwd()),
        preflight_check=False,
    )
    assert res.ok
    # the recovered node's claude invocation carried --resume <id>
    assert seen["command"][:3] == ("claude", "--resume", "sess-resume-9")


def test_dispatch_model_no_resume_turn_when_env_unset(monkeypatch):
    seen = {}

    def spy_dispatch(request, command, **kw):
        seen["command"] = tuple(command)
        return DispatchResult(ok=True, rc=0, model=request.model)

    def fake_resolve(model, root=None):
        return P.ProviderSpec(model=model,
                              command=("claude", "--print", "--output-format", "json"),
                              parse_session=P.claude_session_id)

    monkeypatch.setattr(P, "dispatch", spy_dispatch)
    monkeypatch.setattr(P, "resolve_provider", fake_resolve)
    monkeypatch.setenv("MO_TOOL_GRANTS_DISABLED", "1")
    monkeypatch.delenv("MO_RESUME_SESSION_ID", raising=False)
    monkeypatch.setenv("MO_TARGET_CWD", os.getcwd())

    P.dispatch_model(DispatchRequest(model="minimax", prompt="hi", cwd=os.getcwd()),
                     preflight_check=False)
    assert "--resume" not in seen["command"]


# ── core.dispatch captures the session id from a stub claude end-to-end ─────
def test_core_dispatch_captures_session_from_stub(tmp_path):
    stub = tmp_path / "stub_claude.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "{\\"result\\":\\"ok\\",\\"session_id\\":\\"sess-stub-7\\",'
        '\\"total_cost_usd\\":0,\\"usage\\":{}}"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    res = C.dispatch(
        DispatchRequest(model="minimax", prompt="hi"),
        (str(stub),),
        parse_text=P.claude_result_text,
        parse_session=P.claude_session_id,
    )
    assert res.ok
    assert res.session_id == "sess-stub-7"
    assert res.text == "ok"
