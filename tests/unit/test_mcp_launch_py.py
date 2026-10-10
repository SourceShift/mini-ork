"""``mini-ork mcp launch <name>`` — the shim Zed spawns in place of a server.

Zed cannot hold a secret and does not interpolate ``${NAME}``, so the entry it
is given starts this shim; the shim resolves the registry entry against the
vault and ``execvp``s the real server. These tests pin the two things that make
that safe: the child gets the resolved environment, and every failure happens
on stderr with an empty stdout — stdout is the MCP transport, so a stray byte
there is a protocol error.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.cli import mcp_cmd


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A MINI_ORK_HOME with an empty registry, isolated from the real environment."""
    for name in ("MINI_ORK_RUN_DIR", "MINI_ORK_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_registry(home: Path, servers: dict) -> None:
    (home / "config" / "mcp_servers.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture the execvpe call instead of replacing the test process."""
    calls: list = []

    def fake_execvpe(file, argv, env):
        calls.append((file, argv, env))

    monkeypatch.setattr("os.execvpe", fake_execvpe)
    monkeypatch.setattr(
        "mini_ork.cli.mcp_cmd.shutil.which", lambda name: f"/fake/bin/{name}"
    )
    return calls


def test_launch_execs_the_stdio_server_with_resolved_env(home, execs, monkeypatch):
    monkeypatch.setenv("LIBWIT_ARXIV_TOKEN", "tok-123")
    _write_registry(home, {
        "arxiv": {
            "command": "npx",
            "args": ["-y", "arxiv-mcp-server"],
            "env": {"ARXIV_API_TOKEN": "${LIBWIT_ARXIV_TOKEN}"},
        }
    })

    mcp_cmd.main(["launch", "arxiv"], root=str(home))

    assert len(execs) == 1
    file, argv, env = execs[0]
    assert file == "/fake/bin/npx"
    assert argv == ["npx", "-y", "arxiv-mcp-server"]
    assert env["ARXIV_API_TOKEN"] == "tok-123"


def test_launch_entry_env_wins_over_the_ambient_value(home, execs, monkeypatch):
    monkeypatch.setenv("TOKEN", "ambient")
    _write_registry(home, {
        "srv": {"command": "run", "env": {"TOKEN": "from-entry"}},
    })

    mcp_cmd.main(["launch", "srv"], root=str(home))

    assert execs[0][2]["TOKEN"] == "from-entry"


def test_launch_an_unset_placeholder_fails_before_spawning(home, execs, capsys):
    _write_registry(home, {
        "arxiv": {"command": "npx", "env": {"ARXIV_API_TOKEN": "${NOT_SET_ANYWHERE}"}},
    })

    assert mcp_cmd.main(["launch", "arxiv"], root=str(home)) == 2
    captured = capsys.readouterr()
    assert "NOT_SET_ANYWHERE" in captured.err
    assert captured.out == ""
    assert execs == []


def test_launch_unknown_server_is_refused(home, execs, capsys):
    assert mcp_cmd.main(["launch", "nope"], root=str(home)) == 2
    assert "not in the registry" in capsys.readouterr().err
    assert execs == []


def test_launch_a_url_server_goes_through_the_proxy(home, execs, monkeypatch):
    _write_registry(home, {"remote": {"type": "http", "url": "https://example.test/mcp"}})
    proxied: list[str] = []
    monkeypatch.setattr(
        mcp_cmd, "proxy_server", lambda name, **kw: proxied.append(name) or 0
    )

    assert mcp_cmd.main(["launch", "remote"], root=str(home)) == 0
    assert proxied == ["remote"]
    assert execs == []


def test_proxy_refuses_a_stdio_server(home, capsys):
    _write_registry(home, {"srv": {"command": "npx"}})
    assert mcp_cmd.main(["proxy", "srv"], root=str(home)) == 2
    assert "is a stdio server" in capsys.readouterr().err


def test_launch_a_missing_binary_names_the_command(home, execs, capsys, monkeypatch):
    _write_registry(home, {"srv": {"command": "ghost-tool"}})
    monkeypatch.setattr("mini_ork.cli.mcp_cmd.shutil.which", lambda name: None)

    assert mcp_cmd.main(["launch", "srv"], root=str(home)) == 2
    assert "ghost-tool' is not on PATH" in capsys.readouterr().err
    assert execs == []


def test_launch_never_writes_usage_to_stdout(home, capsys):
    # stdout is the MCP transport; a usage block there would be read as protocol.
    assert mcp_cmd.main(["launch"], root=str(home)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "requires a server name" in captured.err
