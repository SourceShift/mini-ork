"""Unit tests for ``mini_ork.web.orchestrator_shell``.

The Live shell attaches to one long-lived tmux session per project. These
tests pin the pure, side-effect-free pieces that decide *what* runs and *how*
the session/attach argv are shaped:

  * harness resolution precedence: explicit -> config -> env -> default
  * the default is ``opencode`` (a deliberate product decision, not codex)
  * ``.mini-ork/orchestrator.yaml`` parsing is fail-soft (never locks the shell)
  * session names are stable, per-project, and filesystem-safe
  * tmux new-session / attach argv builders (incl. the keep-alive tail)
  * the ``MO_PTY_NO_TMUX`` escape hatch forces the forkpty fallback
"""
from __future__ import annotations

import os
import re

import pytest

from mini_ork.web import orchestrator_shell as osh


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every test starts from a known env: no orchestrator override, tmux on.
    monkeypatch.delenv("MO_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("MO_PTY_NO_TMUX", raising=False)


def _write_config(home, body: str) -> None:
    (home / "orchestrator.yaml").write_text(body, encoding="utf-8")


# ── resolve_harness precedence ──────────────────────────────────────────────

def test_default_is_opencode(tmp_path):
    assert osh.DEFAULT_HARNESS == "opencode"
    assert osh.resolve_harness(None, tmp_path) == "opencode"


def test_explicit_wins_over_config(tmp_path):
    _write_config(tmp_path, "harness: claude\n")
    assert osh.resolve_harness("codex", tmp_path) == "codex"


def test_explicit_invalid_falls_through(tmp_path):
    _write_config(tmp_path, "harness: claude\n")
    # a bogus explicit value is ignored -> config tier wins
    assert osh.resolve_harness("rm-rf", tmp_path) == "claude"


def test_config_wins_over_env(tmp_path, monkeypatch):
    _write_config(tmp_path, "harness: claude\n")
    monkeypatch.setenv("MO_ORCHESTRATOR", "codex")
    assert osh.resolve_harness(None, tmp_path) == "claude"


def test_env_wins_over_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORCHESTRATOR", "codex")
    assert osh.resolve_harness(None, tmp_path) == "codex"


def test_invalid_config_and_env_fall_to_default(tmp_path, monkeypatch):
    _write_config(tmp_path, "harness: bogus\n")
    monkeypatch.setenv("MO_ORCHESTRATOR", "also-bogus")
    assert osh.resolve_harness(None, tmp_path) == "opencode"


def test_resolve_with_no_home(monkeypatch):
    monkeypatch.setenv("MO_ORCHESTRATOR", "claude")
    assert osh.resolve_harness(None, None) == "claude"


# ── config parsing is fail-soft ─────────────────────────────────────────────

def test_read_config_missing_returns_empty(tmp_path):
    assert osh.read_orchestrator_config(tmp_path) == {}


def test_read_config_broken_yaml_returns_empty(tmp_path):
    _write_config(tmp_path, "harness: [unterminated\n")
    assert osh.read_orchestrator_config(tmp_path) == {}


def test_read_config_non_mapping_returns_empty(tmp_path):
    _write_config(tmp_path, "- just\n- a\n- list\n")
    assert osh.read_orchestrator_config(tmp_path) == {}


def test_read_config_good(tmp_path):
    _write_config(tmp_path, "harness: codex\n")
    assert osh.read_orchestrator_config(tmp_path) == {"harness": "codex"}


# ── session_name ────────────────────────────────────────────────────────────

def test_session_name_format(tmp_path):
    name = osh.session_name("opencode", tmp_path)
    assert re.fullmatch(r"mo-opencode-[0-9a-f]{8}", name)


def test_session_name_is_deterministic(tmp_path):
    assert osh.session_name("codex", tmp_path) == osh.session_name("codex", tmp_path)


def test_session_name_differs_by_project(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert osh.session_name("opencode", a) != osh.session_name("opencode", b)


def test_session_name_differs_by_harness(tmp_path):
    assert osh.session_name("codex", tmp_path) != osh.session_name("claude", tmp_path)


# ── harness_argv (forkpty fallback) ─────────────────────────────────────────

def test_harness_argv_shell():
    shell = os.environ.get("SHELL", "/bin/bash")
    assert osh.harness_argv("shell") == [shell, "-l"]


def test_harness_argv_agent():
    assert osh.harness_argv("opencode") == ["opencode"]
    assert osh.harness_argv("codex") == ["codex"]


def test_harness_argv_unknown_is_shell():
    shell = os.environ.get("SHELL", "/bin/bash")
    assert osh.harness_argv("nope") == [shell, "-l"]


# ── tmux argv builders ──────────────────────────────────────────────────────

def test_new_session_argv_agent_has_keepalive(tmp_path):
    argv = osh.build_new_session_argv("mo-opencode-deadbeef", "opencode", tmp_path, 120, 40)
    assert argv[0].endswith("tmux")
    assert argv[1:9] == [
        "new-session", "-d", "-s", "mo-opencode-deadbeef",
        "-x", "120", "-y", "40",
    ]
    assert argv[9:11] == ["-c", str(tmp_path)]
    # runs `<shell> -lc "<harness>; exec ..."` so the pane survives agent exit
    assert argv[-2] == "-lc"
    assert argv[-1].startswith("opencode; exec ")
    assert "exec" in argv[-1]


def test_new_session_argv_shell_is_login_shell(tmp_path):
    argv = osh.build_new_session_argv("mo-shell-cafef00d", "shell", tmp_path, 80, 24)
    # a plain shell gets `-l`, never the `-lc "...; exec"` keep-alive wrapper
    assert argv[-1] == "-l"
    assert "-lc" not in argv


def test_attach_argv():
    argv = osh.build_attach_argv("mo-opencode-deadbeef")
    assert argv[0].endswith("tmux")
    assert argv[1:] == ["attach-session", "-t", "mo-opencode-deadbeef"]


# ── tmux availability escape hatch ──────────────────────────────────────────

def test_no_tmux_env_forces_fallback(monkeypatch):
    monkeypatch.setenv("MO_PTY_NO_TMUX", "1")
    assert osh.tmux_available() is False


def test_tmux_available_gated_on_binary(monkeypatch):
    monkeypatch.setattr(osh.shutil, "which", lambda _name: None)
    assert osh.tmux_available() is False
    monkeypatch.setattr(osh.shutil, "which", lambda _name: "/usr/bin/tmux")
    assert osh.tmux_available() is True
