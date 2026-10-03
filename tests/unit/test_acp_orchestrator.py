"""Hermetic tests for the ACP orchestrator harness (``mini_ork.acp_orchestrator``).

No live lane, no subprocess that touches the real ``claude`` binary: the
``claude`` shim lives on a temp ``PATH`` and emits canned stream-json, and
``resolve_provider`` is monkeypatched where the contract depends on it.
Tests that read config (lane enumeration, default lane) monkeypatch the
registry / ``load_lanes`` so a real ``config/providers.yaml`` is never
touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp_orchestrator import (  # noqa: E402
    build_command,
    default_lane,
    orchestrator_lanes,
)
from mini_ork.acp_orchestrator.harness import run_turn  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FakeProviderSpec:
    command: tuple[str, ...]
    env: dict[str, str]
    model: str = ""


def _fake_spec(command: list[str], env: dict[str, str] | None = None) -> FakeProviderSpec:
    return FakeProviderSpec(
        command=tuple(command),
        env=dict(env or {}),
        model="fake",
    )


def _subscription_spec() -> FakeProviderSpec:
    """Mimic ``opus`` — no ``--model`` flag, ambient auth."""
    return _fake_spec(
        command=[
            "claude",
            "--print",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "json",
        ],
        env={"ANTHROPIC_MODEL": "claude-opus-4-7"},
    )


def _compat_spec() -> FakeProviderSpec:
    """Mimic ``minimax`` — anthropic-compat with ``--model`` injected by env."""
    return _fake_spec(
        command=[
            "claude",
            "--print",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "json",
        ],
        env={
            "ANTHROPIC_AUTH_TOKEN": "fake-token",
            "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
            "ANTHROPIC_MODEL": "MiniMax-M3",
        },
    )


def _codex_spec() -> FakeProviderSpec:
    """Mimic ``codex`` — non-claude CLI, must be excluded by orchestrator_lanes."""
    return _fake_spec(
        command=["codex", "--print"],
        env={},
    )


# ── orchestrator_lanes ──────────────────────────────────────────────────────


def test_orchestrator_lanes_filters_to_claude_family_and_orders_opus_first():
    """Only ``claude``-headed lanes show up; opus pinned first, sonnet second."""
    registry = {
        "opus": {"kind": "anthropic-native", "model": ""},
        "sonnet": {"kind": "anthropic-native", "model": ""},
        "minimax": {
            "kind": "anthropic-compat",
            "model": "MiniMax-M3",
            "base_url": "https://example",
            "api_key_env": "MINIMAX_API_KEY",
        },
        "codex": {"kind": "codex-native", "model": ""},
    }
    with patch(
        "mini_ork.acp_orchestrator.config._load_providers_registry",
        return_value=registry,
    ), patch(
        "mini_ork.acp_orchestrator.config.resolve_provider",
        side_effect=lambda name, *_, **__: {
                "opus": _subscription_spec(),
                "sonnet": _subscription_spec(),
                "minimax": _compat_spec(),
                "codex": _codex_spec(),
            }[name],
    ):
        lanes = orchestrator_lanes()

    ids = [lane["id"] for lane in lanes]
    assert ids[0] == "opus"
    assert "sonnet" in ids[:2]
    # codex heads dropped.
    assert "codex" not in ids
    # The minimax lane uses its registry model name, not the lane id.
    minimax = next(lane for lane in lanes if lane["id"] == "minimax")
    assert minimax["name"] == "MiniMax-M3"
    # opus / sonnet get the friendly label.
    opus = next(lane for lane in lanes if lane["id"] == "opus")
    assert opus["name"] == "Opus (Claude subscription)"


def test_orchestrator_lanes_skips_malformed_registry_entries():
    """A bad entry must not poison the whole picker."""
    registry = {
        "opus": {"kind": "anthropic-native", "model": ""},
        "broken": {"kind": "totally-not-real"},
    }
    with patch(
        "mini_ork.acp_orchestrator.config._load_providers_registry",
        return_value=registry,
    ), patch(
        "mini_ork.acp_orchestrator.config.resolve_provider",
        side_effect=lambda name, *_, **__: _subscription_spec()
        if name == "opus"
        else (_ for _ in ()).throw(ValueError("unknown kind")),
    ):
        lanes = orchestrator_lanes()
    assert [lane["id"] for lane in lanes] == ["opus"]


# ── default_lane ────────────────────────────────────────────────────────────


def test_default_lane_precedence(monkeypatch):
    """env > lane map > "opus"."""
    # No env → lane map wins.
    monkeypatch.delenv("MO_ORCHESTRATOR_LANE", raising=False)
    with patch(
        "mini_ork.acp_orchestrator.config.load_lanes",
        return_value={"orchestrator": "minimax"},
    ), patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "minimax", "name": "MiniMax-M3"}, {"id": "opus", "name": "Opus"}],
    ):
        assert default_lane() == "minimax"

    # env wins over lane map.
    monkeypatch.setenv("MO_ORCHESTRATOR_LANE", "opus")
    with patch(
        "mini_ork.acp_orchestrator.config.load_lanes",
        return_value={"orchestrator": "minimax"},
    ), patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "minimax", "name": "MiniMax-M3"}, {"id": "opus", "name": "Opus"}],
    ):
        assert default_lane() == "opus"


def test_default_lane_falls_back_to_opus_for_unknown(monkeypatch):
    """A bogus ``MO_ORCHESTRATOR_LANE`` lands on ``opus`` with a stderr warning."""
    monkeypatch.setenv("MO_ORCHESTRATOR_LANE", "totally-fake")
    with patch(
        "mini_ork.acp_orchestrator.config.orchestrator_lanes",
        return_value=[{"id": "opus", "name": "Opus"}],
    ):
        result = default_lane()
    assert result == "opus"


# ── build_command ───────────────────────────────────────────────────────────


def _prompt_and_mcp(tmp_path: Path) -> tuple[Path, Path]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("orchestrator system prompt\n")
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}")
    return prompt, mcp


def test_build_command_for_subscription_lane(tmp_path):
    prompt, mcp = _prompt_and_mcp(tmp_path)
    with patch(
        "mini_ork.acp_orchestrator.harness.resolve_provider",
        return_value=_subscription_spec(),
    ):
        argv, env = build_command("opus", resume=None, mcp_config_path=mcp, prompt_path=prompt)

    assert argv[0] == "claude"
    # Output format substitution.
    i = argv.index("--output-format")
    assert argv[i + 1] == "stream-json"
    assert "--verbose" in argv
    # Permission mode substitution.
    pm = argv.index("--permission-mode")
    assert argv[pm + 1] == "default"
    # MCP flags.
    assert "--mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == str(mcp)
    assert "--strict-mcp-config" in argv
    # Tool gating.
    at = argv.index("--allowedTools")
    allowed = argv[at + 1 : argv.index("--disallowedTools")]
    assert "Read" in allowed
    assert "mcp__mini-ork__*" in allowed
    dt = argv.index("--disallowedTools")
    disallowed = argv[dt + 1 : argv.index("--append-system-prompt-file")]
    assert "Edit" in disallowed
    assert "Write" in disallowed
    assert "Bash" in disallowed
    # System prompt file.
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(prompt)
    # No resume when not given.
    assert "--resume" not in argv
    # Env carries the lane's env.
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-7"


def test_build_command_for_compat_lane_includes_model_via_env(tmp_path):
    prompt, mcp = _prompt_and_mcp(tmp_path)
    with patch(
        "mini_ork.acp_orchestrator.harness.resolve_provider",
        return_value=_compat_spec(),
    ):
        _, env = build_command("minimax", resume=None, mcp_config_path=mcp, prompt_path=prompt)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.minimax.io/anthropic"
    assert env["ANTHROPIC_MODEL"] == "MiniMax-M3"


def test_build_command_resume_only_when_given(tmp_path):
    prompt, mcp = _prompt_and_mcp(tmp_path)
    with patch(
        "mini_ork.acp_orchestrator.harness.resolve_provider",
        return_value=_subscription_spec(),
    ):
        argv_no_resume, _ = build_command("opus", resume=None, mcp_config_path=mcp, prompt_path=prompt)
        argv_with_resume, _ = build_command(
            "opus", resume="sess-abc", mcp_config_path=mcp, prompt_path=prompt
        )
    assert "--resume" not in argv_no_resume
    # apply_resume splices right after ``claude`` so the CLI parses it first.
    ri = argv_with_resume.index("--resume")
    assert argv_with_resume[ri + 1] == "sess-abc"
    assert ri > 0 and argv_with_resume[ri - 1] == "claude"


# ── run_turn ────────────────────────────────────────────────────────────────


def _claude_shim(
    tmp_path: Path, body_lines: list[dict[str, Any] | str], *, sleep_s: float = 0.0
) -> Path:
    """Write a fake ``claude`` shell script that prints ``body_lines`` (JSON)
    and exits. If ``sleep_s`` is set, the script sleeps first to force a
    timeout in the harness. ``body_lines`` accepts strings verbatim (raw echo)
    and dicts (JSON-encoded via ``json.dumps``).
    """
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    rendered: list[str] = []
    for line in body_lines:
        if isinstance(line, str):
            # Raw echo: only safe for single-quoted payloads. ``shlex.quote``
            # would also work but we keep the script readable here.
            rendered.append(f"echo {line}")
        else:
            # Wrap each echo in single quotes so bash's brace-expansion +
            # word-split does not eat the JSON quotes. ``json.dumps`` never
            # emits single quotes, so wrapping is safe.
            rendered.append(f"echo '{json.dumps(line)}'")
    body = "\n".join(rendered)
    sleep_line = f"sleep {sleep_s}\n" if sleep_s > 0 else ""
    (shim_dir / "claude").write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f"{sleep_line}"
        f"{body}\n"
    )
    (shim_dir / "claude").chmod(0o755)
    return shim_dir


def _fake_miniork_shim(tmp_path: Path) -> Path:
    """Fake ``mini-ork`` binary the orchestrator writes into its MCP config."""
    shim_dir = tmp_path / "miniork_shim"
    shim_dir.mkdir()
    (shim_dir / "mini-ork").write_text("#!/usr/bin/env bash\nexit 0\n")
    (shim_dir / "mini-ork").chmod(0o755)
    return shim_dir


def test_run_turn_streams_events_and_parses_result(tmp_path, monkeypatch):
    """Three stream-json lines → on_event fires for each, TurnResult is parsed."""
    shim_dir = _claude_shim(
        tmp_path,
        body_lines=[
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "tool_use", "name": "list_runs", "input": {}},
            {
                "type": "result",
                "session_id": "sess-xyz",
                "total_cost_usd": 0.0123,
                "result": "all done",
            },
        ],
    )
    miniork_dir = _fake_miniork_shim(tmp_path)
    # The harness uses ``shutil.which`` + fallbacks. Put both shims on PATH.
    monkeypatch.setenv("PATH", f"{shim_dir}:{miniork_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.delenv("MINI_ORK_VENV_ACTIVE", raising=False)

    events: list[dict] = []

    async def collect(obj: dict) -> None:
        events.append(obj)

    result = asyncio.run(run_turn(
        lane="opus",
        prompt="hello",
        cwd=tmp_path,
        home=tmp_path,
        resume=None,
        on_event=collect,
        timeout_s=10,
    ))

    assert result.rc == 0
    assert result.session_id == "sess-xyz"
    assert result.text == "all done"
    assert result.cost_usd == pytest.approx(0.0123, rel=1e-3)
    assert [e.get("type") for e in events] == ["assistant", "tool_use", "result"]


def test_run_turn_skips_non_json_lines_and_cleans_mcp(tmp_path, monkeypatch):
    """Non-JSON stdout is silently skipped; the temp MCP config is unlinked."""
    shim_dir = _claude_shim(
        tmp_path,
        body_lines=[
            "warning: this is not JSON\n",
            {"type": "result", "session_id": "s1", "result": "ok", "total_cost_usd": 0.0},
        ],
    )
    miniork_dir = _fake_miniork_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shim_dir}:{miniork_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.delenv("MINI_ORK_VENV_ACTIVE", raising=False)

    mcp_path_sentinel: dict[str, Any] = {}

    original_unlink = Path.unlink

    def track_unlink(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "mcp-" in self.name and str(self).endswith(".json"):
            mcp_path_sentinel["path"] = str(self)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", track_unlink)

    result = asyncio.run(run_turn(
        lane="opus",
        prompt="x",
        cwd=tmp_path,
        home=tmp_path,
        resume=None,
        on_event=lambda _: asyncio.sleep(0),
        timeout_s=10,
    ))

    assert result.rc == 0
    assert result.session_id == "s1"
    assert "path" in mcp_path_sentinel
    assert not Path(mcp_path_sentinel["path"]).exists()


def test_run_turn_timeout_yields_rc_124(tmp_path, monkeypatch):
    """A fake ``claude`` that sleeps past the timeout → rc=124, error=timeout."""
    shim_dir = _claude_shim(tmp_path, body_lines=[], sleep_s=5.0)
    miniork_dir = _fake_miniork_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{shim_dir}:{miniork_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.delenv("MINI_ORK_VENV_ACTIVE", raising=False)

    result = asyncio.run(run_turn(
        lane="opus",
        prompt="x",
        cwd=tmp_path,
        home=tmp_path,
        resume=None,
        on_event=lambda _: asyncio.sleep(0),
        timeout_s=0.2,
    ))

    assert result.rc == 124
    assert "timeout" in result.error


def test_run_turn_missing_binary_yields_rc_127(tmp_path, monkeypatch):
    """No ``claude`` on PATH → rc=127."""
    # Create an empty PATH directory so the spawn cannot find a real
    # ``claude`` binary (one might exist on the host PATH and would otherwise
    # be picked up, hanging the test). Drop the inherited PATH so the only
    # search roots are empty + our fake ``mini-ork`` shim.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    miniork_dir = _fake_miniork_shim(tmp_path)
    monkeypatch.setenv("PATH", f"{empty_dir}:{miniork_dir}")
    monkeypatch.delenv("MINI_ORK_VENV_ACTIVE", raising=False)

    result = asyncio.run(run_turn(
        lane="opus",
        prompt="x",
        cwd=tmp_path,
        home=tmp_path,
        resume=None,
        on_event=lambda _: asyncio.sleep(0),
        timeout_s=5,
    ))

    assert result.rc == 127
    assert "spawn failed" in result.error or "No such file" in result.error


# Make ``shutil`` available for any future test that needs it (lint sanity).
_ = shutil

def test_subscription_lane_pins_its_model_by_name(tmp_path):
    from mini_ork.acp_orchestrator.harness import build_command

    cmd, _env = build_command("opus", resume=None, mcp_config_path=tmp_path / "m.json",
                              prompt_path=tmp_path / "p.md")
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_build_command_loads_project_settings_not_the_users(tmp_path, monkeypatch):
    """~/.claude (personal hooks and CLAUDE.md) stays out of the orchestrator;
    the project's settings apply. The env var overrides; empty loads all. A lane
    that already streams (opus/sonnet since Z3) keeps one --output-format and
    drops token-level partials."""
    prompt, mcp = _prompt_and_mcp(tmp_path)
    streaming = _fake_spec(
        command=["claude", "--print", "--permission-mode", "bypassPermissions",
                 "--output-format", "stream-json", "--verbose", "--include-partial-messages"],
        env={},
    )

    def argv_for(sources=None):
        monkeypatch.delenv("MO_ORCHESTRATOR_SETTING_SOURCES", raising=False)
        if sources is not None:
            monkeypatch.setenv("MO_ORCHESTRATOR_SETTING_SOURCES", sources)
        with patch("mini_ork.acp_orchestrator.harness.resolve_provider", return_value=streaming):
            return build_command("opus", resume=None, mcp_config_path=mcp, prompt_path=prompt)[0]

    argv = argv_for()
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    assert "--include-partial-messages" not in argv
    assert argv.count("--output-format") == 1
    argv = argv_for("user,project,local")
    assert argv[argv.index("--setting-sources") + 1] == "user,project,local"
    assert "--setting-sources" not in argv_for("")
