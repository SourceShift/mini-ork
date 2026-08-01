"""One orchestrator per project, backed by a named tmux session.

The old Live-shell forkpty()'d the agent *inside* the WebSocket handler, so the
agent was a child of the request: drop the socket (sleep, closed tab) and the
agent died. The fix, ported from the miniorkv2 reference, is to decouple the
session from the socket:

    * each project gets ONE long-lived tmux session named ``mo-<harness>-<hash>``
    * the session runs the harness (opencode / codex / claude) or a plain shell
    * every WebSocket just ``tmux attach-session`` — a thin client

tmux owns the screen state, so this buys three things for free: the session
survives reconnects, reattaching replays scrollback, and multiple clients can
watch the same pane. When tmux is unavailable (or ``MO_PTY_NO_TMUX`` is set) the
caller falls back to launching the harness argv directly under forkpty — no
persistence, but the shell still works.

Config precedence for which harness a project runs, highest first:

    1. explicit ``cmd`` query param (allow-listed)
    2. ``.mini-ork/orchestrator.yaml`` -> ``harness:``
    3. ``MO_ORCHESTRATOR`` env var
    4. DEFAULT_HARNESS ("opencode")
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_HARNESS = "opencode"
ALLOWED_HARNESSES = ("opencode", "codex", "claude", "shell")

_FALSEY = ("", "0", "false", "no", "off")


def _shell() -> str:
    return os.environ.get("SHELL", "/bin/bash")


def harness_argv(harness: str) -> list[str]:
    """Direct argv for a harness — the forkpty fallback when tmux is absent.

    ``shell`` becomes a login shell; an agent becomes just its own binary. This
    is an allow-list (a ``cmd`` param selects a program, never smuggles argv).
    """
    if harness == "shell":
        return [_shell(), "-l"]
    if harness in ALLOWED_HARNESSES:
        return [harness]
    return [_shell(), "-l"]


def read_orchestrator_config(home: Path) -> dict:
    """Parse ``<home>/orchestrator.yaml`` (home is the ``.mini-ork`` dir).

    Returns ``{}`` on any problem — a broken config must never lock the shell
    out; it just falls through to the next precedence tier.
    """
    path = Path(home) / "orchestrator.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_harness(explicit: str | None, home: Path | None) -> str:
    """Pick the harness by precedence: explicit -> config -> env -> default.

    Anything not in ``ALLOWED_HARNESSES`` is ignored at each tier, so a stale
    config or a typo'd query param can never launch an un-vetted program.
    """
    if explicit and explicit in ALLOWED_HARNESSES:
        return explicit
    if home is not None:
        cfg = read_orchestrator_config(home)
        cfg_harness = cfg.get("harness")
        if isinstance(cfg_harness, str) and cfg_harness in ALLOWED_HARNESSES:
            return cfg_harness
    env_harness = os.environ.get("MO_ORCHESTRATOR", "").strip()
    if env_harness in ALLOWED_HARNESSES:
        return env_harness
    return DEFAULT_HARNESS


def session_name(harness: str, home: Path) -> str:
    """Stable per-project session id: ``mo-<harness>-<8 hex of home path>``.

    Hashing the resolved home keeps names filesystem-safe and collision-free
    across projects while staying deterministic — the same project always
    reattaches to the same session.
    """
    digest = hashlib.sha1(str(Path(home).resolve()).encode()).hexdigest()[:8]
    return f"mo-{harness}-{digest}"


def _tmux_bin() -> str | None:
    return shutil.which("tmux")


def tmux_available() -> bool:
    """True iff tmux should back the session. ``MO_PTY_NO_TMUX`` forces off."""
    if os.environ.get("MO_PTY_NO_TMUX", "").strip().lower() not in _FALSEY:
        return False
    return _tmux_bin() is not None


def _launch_command(harness: str) -> str | None:
    """Shell command run inside a fresh tmux pane, or None for a plain shell.

    Agents get a keep-alive tail: when the agent exits we ``exec`` an
    interactive shell in the *same* pane, so the session (and its scrollback)
    survives an agent restart instead of collapsing to a dead window.
    """
    if harness == "shell":
        return None
    return f'{harness}; exec "${{SHELL:-/bin/sh}}" -i'


def build_new_session_argv(
    name: str, harness: str, cwd: Path, cols: int, rows: int
) -> list[str]:
    """``tmux new-session -d`` argv that spawns the project's orchestrator."""
    tmux = _tmux_bin() or "tmux"
    base = [
        tmux,
        "new-session",
        "-d",
        "-s",
        name,
        "-x",
        str(cols),
        "-y",
        str(rows),
        "-c",
        str(cwd),
    ]
    launch = _launch_command(harness)
    if launch is None:
        return [*base, _shell(), "-l"]
    return [*base, _shell(), "-lc", launch]


def build_attach_argv(name: str) -> list[str]:
    """``tmux attach-session`` argv — what every WebSocket forkpty()'s into."""
    tmux = _tmux_bin() or "tmux"
    return [tmux, "attach-session", "-t", name]


def tmux_has_session(name: str) -> bool:
    tmux = _tmux_bin()
    if tmux is None:
        return False
    try:
        rc = subprocess.run(
            [tmux, "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
    except OSError:
        return False
    return rc == 0


def ensure_session(
    name: str, harness: str, cwd: Path, cols: int, rows: int
) -> None:
    """Idempotent spawn: create the session only if it isn't already alive."""
    if tmux_has_session(name):
        return
    argv = build_new_session_argv(name, harness, cwd, cols, rows)
    subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
