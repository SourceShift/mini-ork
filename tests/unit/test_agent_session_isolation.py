"""Agent-session isolation — every claude-CLI node mini-ork spawns runs with
mini-ork's OWN settings, not the operator's personal ``~/.claude`` config
(kickoff/agent-session-isolation.md).

The load-bearing contract: a claude argv carries
``--setting-sources project,local --settings <repo>/config/agent-claude-settings.json``
(plus ``--model opus|sonnet`` for the two subscription lanes) so the operator's
SessionStart hooks / output-style rules / personal CLAUDE.md never load.
``MO_NODE_SETTING_SOURCES=""`` restores the legacy "load everything" behaviour.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mini_ork.dispatch.models import DispatchRequest
from mini_ork.dispatch.providers import (
    _claude_command_builder,
    claude_isolation_args,
)

REPO = Path(__file__).resolve().parents[2]
SETTINGS = REPO / "config" / "agent-claude-settings.json"
CLAUDE_ARGV = ("claude", "-p", "--output-format", "json")


def _req(model: str) -> DispatchRequest:
    return DispatchRequest(model=model, prompt="hi")


# ── defaults: the flags land, in the right place ─────────────────────────────


def test_builder_default_lane_gets_isolation_flags():
    out = _claude_command_builder(
        CLAUDE_ARGV, request=_req("glm"), env={"MO_TOOL_GRANTS_DISABLED": "1"}
    )
    assert "--setting-sources" in out
    assert out[out.index("--setting-sources") + 1] == "project,local"
    assert out[out.index("--settings") + 1] == str(SETTINGS)
    # Positioned before --output-format (the builder's positional contract).
    assert out.index("--setting-sources") < out.index("--output-format")
    assert out.index("--settings") < out.index("--output-format")
    # A non-subscription lane pins no model.
    assert "--model" not in out


def test_settings_file_exists_and_re_pins_user_values():
    data = json.loads(SETTINGS.read_text())
    assert data["effortLevel"] == "xhigh"
    assert data["alwaysThinkingEnabled"] is True
    # The API timeout the user setting supplied must survive the source switch:
    # without it the CLI default (600000 ms) applies to every lane, and slow
    # gateway lanes at xhigh effort can hit the timeout.
    assert data["env"]["API_TIMEOUT_MS"] == "3000000"
    # Auto-memory resolves through the git common dir, so a node in a mini-ork
    # worktree would load (and write) the operator's project MEMORY.md even
    # under --setting-sources project,local. Agents get verified context only.
    assert data["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    bash_hooks = [h for h in data["hooks"]["PreToolUse"] if h.get("matcher") == "Bash"]
    assert bash_hooks, "expected a PreToolUse/Bash hook"
    command = bash_hooks[0]["hooks"][0]["command"]
    # rtk compaction stays, but ONLY when rtk is installed (else a no-op drain).
    assert "command -v rtk" in command and "rtk hook claude" in command


# ── subscription lanes: the model pin the user setting used to supply ─────────


def test_builder_pins_subscription_model_for_opus_lane():
    out = _claude_command_builder(
        CLAUDE_ARGV, request=_req("opus"), env={"MO_TOOL_GRANTS_DISABLED": "1"}
    )
    assert out[out.index("--model") + 1] == "opus"


def test_builder_does_not_pin_model_when_spawn_env_already_sets_one():
    # The helper honours an ANTHROPIC_MODEL on the CHILD's spawn env: if the
    # lane genuinely exports one, --model would override it (the flag wins over
    # env), so it is skipped. (dispatch_model passes the post-merge env — see
    # test_dispatch_pins_subscription_model_despite_ambient_anthropic_model.)
    out = _claude_command_builder(
        CLAUDE_ARGV,
        request=_req("opus"),
        env={"MO_TOOL_GRANTS_DISABLED": "1", "ANTHROPIC_MODEL": "claude-x"},
    )
    assert "--model" not in out


def test_builder_does_not_duplicate_an_existing_model_flag():
    argv = ("claude", "-p", "--model", "custom", "--output-format", "json")
    out = _claude_command_builder(
        argv, request=_req("opus"), env={"MO_TOOL_GRANTS_DISABLED": "1"}
    )
    assert out.count("--model") == 1
    assert out[out.index("--model") + 1] == "custom"


def test_dispatch_pins_subscription_model_despite_ambient_anthropic_model(monkeypatch):
    """Regression: the pin must read the CHILD's spawn env, not the ambient one.

    ``dispatch_model`` passes the post-merge env (after ``spec.unset_env`` strips
    ANTHROPIC_GATEWAY_ENV) to the command builder, so an ambient
    ANTHROPIC_MODEL the child never sees can no longer suppress ``--model opus``
    — which had left the opus/sonnet lanes on the CLI default model.
    """
    from mini_ork.dispatch import providers
    from mini_ork.dispatch.models import DispatchResult

    spec = providers.ProviderSpec(
        model="opus",
        command=(
            "claude", "--print", "--permission-mode", "bypassPermissions",
            "--output-format", "json",
        ),
        kind="claude",
        unset_env=providers.ANTHROPIC_GATEWAY_ENV,
    )
    monkeypatch.setattr(providers, "resolve_provider", lambda *a, **k: spec)
    monkeypatch.setenv("ANTHROPIC_MODEL", "glm-5.1")  # ambient; stripped for the child

    captured: dict[str, tuple[str, ...]] = {}

    def backend(request, spec_):
        captured["cmd"] = tuple(spec_.command)
        return DispatchResult(ok=True, rc=0, text="ok", model=request.model)

    monkeypatch.setitem(providers.MODEL_DISPATCH_BACKENDS, "opus", backend)
    result = providers.dispatch_model(
        DispatchRequest(model="opus", prompt="hi"), None, preflight_check=False
    )

    assert result.ok, result.error
    cmd = captured["cmd"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"


# ── the kill switch and the grants interaction ───────────────────────────────


def test_empty_setting_sources_disables_isolation():
    out = _claude_command_builder(
        CLAUDE_ARGV,
        request=_req("glm"),
        env={"MO_TOOL_GRANTS_DISABLED": "1", "MO_NODE_SETTING_SOURCES": ""},
    )
    assert out == CLAUDE_ARGV


def test_isolation_applies_while_tool_grants_still_run():
    # Isolation is orthogonal to tool access: it is applied regardless of the
    # grants flag, and grants keep working when they are enabled.
    out = _claude_command_builder(
        CLAUDE_ARGV, request=_req("glm"), env={"MO_RESOLVED_NODE_TOOLS": "Read|"}
    )
    assert "--setting-sources" in out
    assert "--allowedTools" in out


# ── the pure helper: overrides, de-dup, non-claude pass-through ──────────────


def test_helper_does_not_duplicate_an_existing_setting_sources():
    argv = ("claude", "-p", "--setting-sources", "user", "--output-format", "json")
    flags = claude_isolation_args({}, "glm", argv)
    assert "--setting-sources" not in flags
    assert "--settings" in flags


def test_node_settings_override_and_empty_omits_the_flag(tmp_path):
    custom = tmp_path / "custom.json"
    custom.write_text("{}")
    flags = claude_isolation_args({"MO_NODE_SETTINGS": str(custom)}, "glm", CLAUDE_ARGV)
    assert flags[flags.index("--settings") + 1] == str(custom)
    flags2 = claude_isolation_args({"MO_NODE_SETTINGS": ""}, "glm", CLAUDE_ARGV)
    assert "--settings" not in flags2


def test_missing_settings_file_omits_the_flag():
    # `claude -p --settings <missing>` hard-fails the node ("Settings file not
    # found"); a non-checkout install has no repo-root config/, so the flag is
    # omitted rather than crashing every claude node. --setting-sources stays.
    missing = "/nonexistent/agent-claude-settings.json"
    flags = claude_isolation_args({"MO_NODE_SETTINGS": missing}, "glm", CLAUDE_ARGV)
    assert "--settings" not in flags
    assert "--setting-sources" in flags


def test_non_claude_argv_is_untouched():
    argv = ("codex", "exec", "--json")
    assert claude_isolation_args({}, "codex", argv) == []
    assert _claude_command_builder(argv, request=_req("codex"), env={}) == argv


# ── the two other call sites: rubric pre-screen + cleaner worker ─────────────


def test_cleaner_worker_argv_carries_isolation(monkeypatch, tmp_path):
    from mini_ork.recovery import cleaner

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cleaner.subprocess, "run", fake_run)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the thing")
    rc = cleaner._default_spawn(str(tmp_path), str(prompt), str(tmp_path), "glm", 1.0, None)

    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--settings") + 1] == str(SETTINGS)
    # The existing --model stays; the subscription alias must not duplicate it.
    assert cmd.count("--model") == 1
    assert cmd[cmd.index("--model") + 1] == "glm"


def test_rubric_argv_carries_isolation(monkeypatch, tmp_path):
    from mini_ork.gates import rubric_prescreen as rp
    from mini_ork.stores.migrate import init_db

    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    rc, _out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, err

    kickoff = tmp_path / "kickoff.md"
    kickoff.write_text("kickoff body")
    con = sqlite3.connect(dbp)
    con.execute(
        "INSERT INTO epics (id, title, status, kickoff_path) VALUES (?, ?, ?, ?)",
        # kickoff_path is resolved RELATIVE to repo_root (rubric_cache line ~80).
        ("E-ISO", "isolation", "in progress", kickoff.name),
    )
    con.commit()
    con.close()

    captured: dict[str, list[list[str]]] = {}

    def fake_run(cmd, **_kwargs):
        captured.setdefault("cmds", []).append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    # The rubric resolves the provider registry relative to
    # dirname(dirname(scripts_dir)); point it at a nested path so _lane_root
    # lands on REPO and config/providers.yaml is found without an ambient
    # MINI_ORK_PROVIDERS override.
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)

    rp.mo_run_rubric_prescreen(
        "E-ISO",
        str(REPO),
        1,
        str(tmp_path),
        str(tmp_path / "prompts"),
        str(REPO / "lib" / "scripts"),
        mini_ork_home=str(home),
        mini_ork_db=dbp,
        skip_cache=True,
        lane="glm",
    )

    claude_cmds = [c for c in captured.get("cmds", []) if c[:1] == ["claude"]]
    assert claude_cmds, "expected the rubric to invoke claude"
    cmd = claude_cmds[0]
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--settings") + 1] == str(SETTINGS)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
