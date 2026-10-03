"""Hermetic tests for ``mini-ork zed setup|status|uninstall``.

Every test points ``$ZED_SETTINGS`` at a tmp file so the real
``~/.config/zed/settings.json`` is never touched. ``$MINI_ORK_ROOT`` is
likewise pointed at a tmp dir so ``_resolve_launcher_path`` falls through
to its third option (``<root>/bin/mini-ork``) deterministically.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mini_ork.cli import zed_cmd


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    """A temp file used as ``$ZED_SETTINGS`` for the duration of a test."""
    return tmp_path / "settings.json"


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake engine root with a stub ``bin/mini-ork`` so launcher resolution is deterministic.

    We write a tiny executable shell script: the bytes do not need to be a real
    Python launcher, only an absolute path that ``os.path.exists`` and
    ``os.access(... X_OK)`` accept. Tests that need to override the launcher
    further can monkeypatch ``_resolve_launcher_path``.
    """
    root = tmp_path / "engine_root"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "mini-ork"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("ZED_SETTINGS", raising=False)
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    return root


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def test_setup_creates_both_entries_with_absolute_command(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))
    rc = zed_cmd.main(["setup"], root=str(fake_root))
    assert rc == 0
    data = _read(settings_file)
    agent = data["agent_servers"]["mini-ork"]
    context = data["context_servers"]["mini-ork"]
    assert agent["type"] == "custom"
    assert agent["args"] == ["acp"]
    assert agent["env"] == {}
    assert context["args"] == ["mcp-context"]
    assert context["env"] == {}
    # The launcher path must be absolute (macGUI apps do not inherit shell PATH).
    assert os.path.isabs(agent["command"])
    assert os.path.isabs(context["command"])
    assert agent["command"] == context["command"]


def test_setup_preserves_unrelated_keys_and_existing_agent(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = {
        "theme": "solarized-dark",
        "agent_servers": {"other-agent": {"type": "custom", "command": "/usr/bin/other"}},
        "context_servers": {"other-ctx": {"command": "/usr/bin/other", "args": ["x"]}},
        "ui": {"font_size": 12},
    }
    settings_file.write_text(json.dumps(existing))
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))

    rc = zed_cmd.main(["setup"], root=str(fake_root))
    assert rc == 0

    data = _read(settings_file)
    # Other top-level keys survive untouched.
    assert data["theme"] == "solarized-dark"
    assert data["ui"] == {"font_size": 12}
    # Pre-existing unrelated entries survive untouched.
    assert data["agent_servers"]["other-agent"] == existing["agent_servers"]["other-agent"]
    assert data["context_servers"]["other-ctx"] == existing["context_servers"]["other-ctx"]
    # New entries coexist.
    assert "mini-ork" in data["agent_servers"]
    assert "mini-ork" in data["context_servers"]


def test_setup_creates_timestamped_backup(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file.write_text(json.dumps({"theme": "dark"}))
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))

    rc = zed_cmd.main(["setup"], root=str(fake_root))
    assert rc == 0

    backups = list(settings_file.parent.glob("settings.json.bak-*"))
    assert backups, "expected a backup file matching settings.json.bak-*"
    backup = backups[0]
    backup_data = json.loads(backup.read_text())
    assert backup_data == {"theme": "dark"}


def test_setup_home_adds_mini_ork_home_to_both_envs(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_home = fake_root / "project_home"
    project_home.mkdir()
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))

    rc = zed_cmd.main(["setup", "--home", str(project_home)], root=str(fake_root))
    assert rc == 0

    data = _read(settings_file)
    assert data["agent_servers"]["mini-ork"]["env"] == {
        "MINI_ORK_HOME": str(project_home.resolve())
    }
    assert data["context_servers"]["mini-ork"]["env"] == {
        "MINI_ORK_HOME": str(project_home.resolve())
    }


def test_setup_dry_run_writes_nothing(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))
    assert not settings_file.exists()

    rc = zed_cmd.main(["setup", "--dry-run"], root=str(fake_root))
    assert rc == 0
    assert not settings_file.exists()
    assert not list(settings_file.parent.glob("settings.json.bak-*"))


def test_setup_parse_failure_leaves_file_byte_identical_and_returns_2(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Zed accepts // comments and trailing commas; the kickoff contract is to
    # NOT silently overwrite the file. We use a fully-unparseable payload
    # (unterminated string) so the parse-failure path actually triggers.
    original = '{"theme": "dark",\n// a comment\n"broken": "unterminated'
    settings_file.write_text(original)
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))

    rc = zed_cmd.main(["setup"], root=str(fake_root))
    captured = capsys.readouterr()
    assert rc == 2
    # File is left untouched (byte-identical to what we wrote).
    assert settings_file.read_text() == original
    # The two manual-merge blocks are printed to stderr so the user can merge by hand.
    assert "agent_servers" in captured.err
    assert "context_servers" in captured.err
    assert "mini-ork" in captured.err


_COMMENTED = '{\n  // my theme\n  "theme": "dark",\n  "agent_servers": {\n    "mini-ork": {"type": "custom", "command": "/x", "args": ["acp"], "env": {}},\n  },\n}\n'


@pytest.mark.parametrize("sub", ["setup", "uninstall"])
def test_writers_never_rewrite_a_commented_settings_file(
    sub: str, settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Valid Zed settings with // comments and trailing commas (Zed's own template
    # looks like this). Rewriting it would silently delete the user's comments.
    settings_file.write_text(_COMMENTED)
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))
    rc = zed_cmd.main([sub], root=str(fake_root))
    assert rc == 2
    assert settings_file.read_text() == _COMMENTED
    assert not list(settings_file.parent.glob("settings.json.bak-*"))
    if sub == "setup":
        err = capsys.readouterr().err
        assert "agent_servers" in err and "context_servers" in err


def test_status_reads_a_commented_settings_file(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings_file.write_text(_COMMENTED)
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))
    assert zed_cmd.main(["status"], root=str(fake_root)) == 0
    out = capsys.readouterr().out
    assert "agent_servers.mini-ork: configured" in out
    assert "context_servers.mini-ork: not configured" in out
    assert settings_file.read_text() == _COMMENTED


def test_status_reports_configured_and_not_configured(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))

    # 1. Not configured yet — file does not exist.
    rc = zed_cmd.main(["status"], root=str(fake_root))
    assert rc == 0
    out = capsys.readouterr().out
    assert str(settings_file) in out
    assert "agent_servers.mini-ork: not configured" in out
    assert "context_servers.mini-ork: not configured" in out

    # 2. After setup, status should report "configured" for both.
    capsys.readouterr()  # discard
    zed_cmd.main(["setup"], root=str(fake_root))
    rc = zed_cmd.main(["status"], root=str(fake_root))
    assert rc == 0
    out = capsys.readouterr().out
    assert "agent_servers.mini-ork: configured" in out
    assert "context_servers.mini-ork: configured" in out
    assert "command exists + executable: yes" in out


def test_uninstall_removes_only_mini_ork_entries_and_drops_empty_maps(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = {
        "theme": "dark",
        "agent_servers": {
            "mini-ork": {"type": "custom", "command": "/x", "args": ["acp"], "env": {}},
            "other": {"type": "custom", "command": "/y", "args": [], "env": {}},
        },
        "context_servers": {
            "mini-ork": {"command": "/x", "args": ["mcp-context"], "env": {}},
        },
    }
    settings_file.write_text(json.dumps(initial))
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))

    rc = zed_cmd.main(["uninstall"], root=str(fake_root))
    assert rc == 0

    data = _read(settings_file)
    # Theme survives.
    assert data["theme"] == "dark"
    # Other agent survives.
    assert "other" in data["agent_servers"]
    assert "mini-ork" not in data["agent_servers"]
    # context_servers was empty after removal and is dropped.
    assert "context_servers" not in data


def test_uninstall_with_nothing_to_remove_is_zero(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file.write_text(json.dumps({"theme": "dark"}))
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))

    rc = zed_cmd.main(["uninstall"], root=str(fake_root))
    assert rc == 0


def test_unknown_subcommand_returns_2(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))
    rc = zed_cmd.main(["bogus"], root=str(fake_root))
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown subcommand" in captured.err


def test_help_returns_zero_and_prints_usage(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))
    rc = zed_cmd.main(["--help"], root=str(fake_root))
    captured = capsys.readouterr()
    assert rc == 0
    assert "Usage: mini-ork zed" in captured.err
    assert "setup" in captured.err
    assert "status" in captured.err
    assert "uninstall" in captured.err


def test_no_args_returns_2_and_prints_usage(
    settings_file: Path, fake_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ZED_SETTINGS", str(settings_file))
    rc = zed_cmd.main([], root=str(fake_root))
    captured = capsys.readouterr()
    assert rc == 2
    assert "Usage: mini-ork zed" in captured.err