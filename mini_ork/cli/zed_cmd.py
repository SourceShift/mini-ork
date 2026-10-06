"""``mini-ork zed`` — install, inspect, and remove the mini-ork entries in
Zed's ``settings.json``.

Zed's External Agent and MCP context server surfaces already speak
``mini-ork acp`` and ``mini-ork mcp-context``; this command is the
user-facing one-click wiring for both. It edits a settings file the user
controls, so every write is preceded by a timestamped backup and parses
the existing JSON defensively (Zed accepts ``//`` comments and trailing
commas, which :func:`json.loads` rejects). The dispatcher at
``_native_module_handler`` in :mod:`mini_ork.cli.main` runs this module as
``python -m mini_ork.cli.zed_cmd`` with ``PYTHONSAFEPATH=1``, so every
diagnostic this module emits goes to stderr.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import re
import shutil
import sys
from typing import Any

#: Absolute path to the ``settings.json`` that Zed reads at launch. Overridable
#: for tests and CI hermetic runs via ``$ZED_SETTINGS``.
_DEFAULT_SETTINGS_PATH = "~/.config/zed/settings.json"

#: Filename pattern for the timestamped backup written before every mutation.
_BACKUP_SUFFIX_FMT = "%Y%m%d-%H%M%S"

#: Fixed ``settings.json`` blocks we merge when the existing file is unparseable.
_MANUAL_AGENT_BLOCK = (
    '"agent_servers": { "mini-ork": '
    '{ "type": "custom", "command": "<mini-ork>", "args": ["acp"], "env": {} } },'
)
_MANUAL_CONTEXT_BLOCK = (
    '"context_servers": { "mini-ork": '
    '{ "command": "<mini-ork>", "args": ["mcp-context"], "env": {} } }'
)

_USAGE = (
    "Usage: mini-ork zed <setup|status|uninstall> [--home <dir>] [--dry-run]\n\n"
    "Wire (or unwire) mini-ork inside Zed's settings.json. Reads and writes\n"
    "$ZED_SETTINGS if set, else ~/.config/zed/settings.json. Always backs up\n"
    "the existing file before mutating it.\n"
)


def _settings_path() -> str:
    """Resolve the Zed settings path: ``$ZED_SETTINGS`` or the platform default."""
    env_override = os.environ.get("ZED_SETTINGS")
    return env_override if env_override else os.path.expanduser(_DEFAULT_SETTINGS_PATH)


def _resolve_launcher_path(root: str) -> str:
    """Return the absolute path of the ``mini-ork`` launcher to embed in Zed.

    Order:
      1. ``shutil.which("mini-ork")`` — whatever ``PATH`` exposes first.
      2. ``~/.local/bin/mini-ork`` if it exists — the recommended install.
      3. ``<root>/bin/mini-ork`` — the in-tree launcher.

    macOS GUI apps do not inherit the shell ``PATH``, so a bare ``"mini-ork"``
    would not launch when Zed is started from Finder / Spotlight. Always
    return an absolute path so the embedded command works from any context.
    """
    found = shutil.which("mini-ork")
    if found:
        return os.path.abspath(found)
    home_local = os.path.expanduser("~/.local/bin/mini-ork")
    if os.path.exists(home_local):
        return os.path.abspath(home_local)
    return os.path.abspath(os.path.join(root, "bin", "mini-ork"))


def _warn_missing_acp() -> None:
    """Print a stderr hint when the ``acp`` extra is not importable.

    The ACP runtime is an optional extra (``pip install 'mini-ork[acp]'``);
    without it, ``mini-ork zed setup`` still writes a valid settings file,
    but the agent will refuse to start. Warn, do not fail.
    """
    if importlib.util.find_spec("acp") is None:
        sys.stderr.write(
            "mini-ork zed: the optional 'acp' extra is not installed\n"
            "  Install with: pip install 'mini-ork[acp]'\n"
        )


def _strip_jsonc_noise(text: str) -> str:
    """Best-effort cleanup of Zed's non-strict JSON (line comments + trailing commas).

    We only reach for this when ``json.loads`` already rejected the input, so a
    conservative pass is fine: drop ``//`` line comments, then drop trailing
    commas before ``}`` or ``]``. The result must round-trip through
    ``json.loads``; if it still does not, the caller gives up and returns 2.
    """
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        idx = line.find("//")
        # Only treat ``//`` as a comment when it's outside of any string literal.
        # Heuristic: count unescaped quotes to the left; if even, we're outside.
        if idx != -1:
            prefix = line[:idx]
            if prefix.count('"') % 2 == 0:
                line = prefix.rstrip()
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    # Trailing commas: ``,\s*]`` and ``,\s*}`` patterns.
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def _load_settings(path: str, *, lenient: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """Read + parse a settings file. Returns ``(data, error_message)``.

    Missing file → ``({}, None)`` (treat as empty object, no error). Unparseable
    file → ``(None, "...")`` and the caller prints the manual-merge blocks and
    returns 2 — the kickoff contract forbids silently overwriting an unreadable
    file.
    """
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path) as f:
            text = f.read()
    except OSError as exc:
        return None, f"could not read settings file: {exc}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    if not lenient:
        # Writers stay strict: re-serializing a file that only parsed after
        # stripping would silently delete the user's comments (Zed's default
        # settings template is full of them). They print the blocks instead.
        return None, "settings file has comments or trailing commas (not plain JSON)"
    try:
        return json.loads(_strip_jsonc_noise(text)), None
    except json.JSONDecodeError as exc:
        return None, f"settings file is not valid JSON: {exc}"


def _backup_path(path: str) -> str:
    """Compute the timestamped backup filename alongside ``path``."""
    timestamp = _dt.datetime.now().strftime(_BACKUP_SUFFIX_FMT)
    return f"{path}.bak-{timestamp}"


def _entry_env(home: str | None) -> dict[str, str]:
    """``MINI_ORK_HOME`` when given, plus the ``PATH`` of the shell running setup.

    Zed started from the Dock gives its agents launchd's bare PATH
    (``/usr/bin:/bin:…``), where the ``claude`` CLI the orchestrator runs on
    — and the agent CLIs a run dispatches to — are not found.
    """
    env: dict[str, str] = {}
    if home:
        env["MINI_ORK_HOME"] = os.path.abspath(home)
    if os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    return env


def _build_agent_entry(launcher: str, home: str | None) -> dict[str, Any]:
    """Build the ``agent_servers["mini-ork"]`` block."""
    return {"type": "custom", "command": launcher, "args": ["acp"], "env": _entry_env(home)}


def _build_context_entry(launcher: str, home: str | None) -> dict[str, Any]:
    """Build the ``context_servers["mini-ork"]`` block."""
    return {"command": launcher, "args": ["mcp-context"], "env": _entry_env(home)}


#: ``zed setup --layout``: the task-board arrangement — threads and the
#: agent on the left, the Git panel (the task's files and changes) and the
#: Project panel on the right. Zed's own "Panel Layout > Agentic" does the
#: same for the current window; these keys make it the default.
_LAYOUT_KEYS: tuple[tuple[str, str, str], ...] = (
    ("agent", "dock", "left"),
    ("git_panel", "dock", "right"),
    ("project_panel", "dock", "right"),
)


def _apply_layout(data: dict[str, Any]) -> list[str]:
    """Set the task-board dock positions in ``data``; return the keys changed.

    Only the dock keys are touched — every other panel setting is kept.
    A section that exists but is not an object is left alone.
    """
    changed: list[str] = []
    for section, key, value in _LAYOUT_KEYS:
        block = data.setdefault(section, {})
        if not isinstance(block, dict):
            continue
        if block.get(key) != value:
            block[key] = value
            changed.append(f"{section}.{key} = {value}")
    agent = data.get("agent")
    if isinstance(agent, dict):
        sidebar = agent.setdefault("threads_sidebar", {})
        if isinstance(sidebar, dict) and sidebar.get("position") != "left":
            sidebar["position"] = "left"
            changed.append("agent.threads_sidebar.position = left")
    return changed


def _write_settings(path: str, data: dict[str, Any]) -> str | None:
    """Atomically write ``data`` to ``path`` after backing up the previous file.

    Returns the backup path on success, ``None`` when there was no prior file
    to back up (caller chooses whether to message the user about it).
    """
    backup = None
    if os.path.exists(path):
        backup = _backup_path(path)
        shutil.copy2(path, backup)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)
    return backup


def _cmd_setup(args: argparse.Namespace) -> int:
    """``setup``: merge the two mini-ork entries into the settings file."""
    settings_path = args.settings_path
    data, err = _load_settings(settings_path)
    if data is None:
        sys.stderr.write(
            f"mini-ork zed setup: {err}\n"
            f"Cannot parse the existing settings file. Merge these two blocks by hand:\n"
            f"  {_MANUAL_AGENT_BLOCK}\n"
            f"  {_MANUAL_CONTEXT_BLOCK}\n"
        )
        return 2

    launcher = _resolve_launcher_path(args.root)
    home = args.home

    data.setdefault("agent_servers", {})
    data.setdefault("context_servers", {})
    if not isinstance(data["agent_servers"], dict):
        sys.stderr.write(
            "mini-ork zed setup: existing 'agent_servers' is not an object; "
            "refusing to overwrite\n"
        )
        return 2
    if not isinstance(data["context_servers"], dict):
        sys.stderr.write(
            "mini-ork zed setup: existing 'context_servers' is not an object; "
            "refusing to overwrite\n"
        )
        return 2

    data["agent_servers"]["mini-ork"] = _build_agent_entry(launcher, home)
    data["context_servers"]["mini-ork"] = _build_context_entry(launcher, home)
    layout_changes = _apply_layout(data) if getattr(args, "layout", False) else []

    if args.dry_run:
        sys.stdout.write(
            f"[dry-run] would write to {settings_path}:\n"
            f"{json.dumps(data, indent=2, sort_keys=True)}\n"
        )
        _warn_missing_acp()
        return 0

    backup = _write_settings(settings_path, data)
    if backup:
        sys.stdout.write(f"backup: {backup}\n")
    sys.stdout.write(f"wrote: {settings_path}\n")
    sys.stdout.write(
        f"  agent_servers.mini-ork.command   = {launcher}\n"
        f"  context_servers.mini-ork.command = {launcher}\n"
    )
    if home:
        sys.stdout.write(f"  env.MINI_ORK_HOME (both)         = {os.path.abspath(home)}\n")
    for change in layout_changes:
        sys.stdout.write(f"  {change}\n")
    if getattr(args, "layout", False):
        sys.stdout.write(
            "  layout: threads + agent on the left, Git panel (the task's files and changes)\n"
            "          on the right. In an open window: Panel Layout > Agentic, or the\n"
            "          `workspace: use agentic layout` action. Start each task in its own\n"
            "          worktree from the worktree picker in the title bar.\n"
        )
    if os.environ.get("PATH"):
        sys.stdout.write("  env.PATH (both)                  = this shell's PATH (Zed from the Dock has a bare one;\n"
                         "                                     run setup again after installing a new CLI)\n")
    _warn_missing_acp()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """``status``: report whether the entries are wired up. Always returns 0."""
    settings_path = args.settings_path
    data, err = _load_settings(settings_path, lenient=True)
    if data is None:
        sys.stdout.write(
            f"settings: {settings_path}\n"
            f"  parse error: {err}\n"
        )
        return 0

    sys.stdout.write(f"settings: {settings_path}\n")

    agents = data.get("agent_servers") or {}
    contexts = data.get("context_servers") or {}

    agent_entry = agents.get("mini-ork") if isinstance(agents, dict) else None
    context_entry = contexts.get("mini-ork") if isinstance(contexts, dict) else None

    def _report(label: str, entry: dict[str, Any] | None) -> None:
        if not entry:
            sys.stdout.write(f"  {label}: not configured\n")
            return
        command = entry.get("command") if isinstance(entry, dict) else None
        sys.stdout.write(f"  {label}: configured\n")
        sys.stdout.write(f"    command: {command}\n")
        if command and os.path.isabs(command):
            executable = os.path.exists(command) and os.access(command, os.X_OK)
            sys.stdout.write(
                f"    command exists + executable: {'yes' if executable else 'no'}\n"
            )
        else:
            sys.stdout.write(
                "    warning: command is not absolute; macOS GUI apps may not see PATH\n"
            )

    _report("agent_servers.mini-ork", agent_entry)
    _report("context_servers.mini-ork", context_entry)

    acp_ok = importlib.util.find_spec("acp") is not None
    sys.stdout.write(f"  'acp' extra importable: {'yes' if acp_ok else 'no'}\n")
    if not acp_ok:
        sys.stdout.write("    Install with: pip install 'mini-ork[acp]'\n")

    project_home = os.path.join(os.getcwd(), ".mini-ork")
    sys.stdout.write(
        f"  project home (.mini-ork in cwd): "
        f"{'present' if os.path.isdir(project_home) else 'absent'}\n"
    )
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    """``uninstall``: drop both entries and empty parent maps. Nothing-to-do is 0."""
    settings_path = args.settings_path
    data, err = _load_settings(settings_path)
    if data is None:
        sys.stderr.write(
            f"mini-ork zed uninstall: {err}\n"
            f"Nothing removed. Fix the parse error and re-run, or merge these blocks\n"
            f"to remove by hand:\n"
            f"  (delete the mini-ork entry from agent_servers and context_servers)\n"
        )
        return 2

    agents = data.get("agent_servers") or {}
    contexts = data.get("context_servers") or {}

    removed = False
    if isinstance(agents, dict) and "mini-ork" in agents:
        del agents["mini-ork"]
        removed = True
    if isinstance(contexts, dict) and "mini-ork" in contexts:
        del contexts["mini-ork"]
        removed = True

    # Drop now-empty maps so the resulting settings file stays tidy.
    if isinstance(agents, dict) and not agents:
        data.pop("agent_servers", None)
    if isinstance(contexts, dict) and not contexts:
        data.pop("context_servers", None)

    if args.dry_run:
        sys.stdout.write(
            f"[dry-run] would write to {settings_path}:\n"
            f"{json.dumps(data, indent=2, sort_keys=True)}\n"
        )
        return 0

    if not removed:
        sys.stdout.write(
            f"nothing to remove in {settings_path} (no mini-ork entries present)\n"
        )
        return 0

    backup = _write_settings(settings_path, data)
    if backup:
        sys.stdout.write(f"backup: {backup}\n")
    sys.stdout.write(f"wrote: {settings_path}\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-ork zed",
        description=_USAGE.splitlines()[0],
        add_help=False,
    )
    sub = parser.add_subparsers(dest="subcommand")

    setup = sub.add_parser("setup", add_help=False)
    setup.add_argument("--home", default=None, help="absolute MINI_ORK_HOME to embed")
    setup.add_argument(
        "--dry-run", action="store_true", help="print the resulting JSON, write nothing"
    )
    setup.add_argument(
        "--layout", action="store_true",
        help="also dock threads + agent left and the Git/Project panels right",
    )

    sub.add_parser("status", add_help=False)

    uninstall = sub.add_parser("uninstall", add_help=False)
    uninstall.add_argument(
        "--dry-run", action="store_true", help="print the resulting JSON, write nothing"
    )
    return parser


def main(rest: list[str], root: str) -> int:
    """Entry point invoked by ``mini-ork zed ...`` (see ``_native_module_handler``)."""
    if rest and rest[0] in ("--help", "-h"):
        sys.stderr.write(_USAGE)
        return 0
    if not rest:
        sys.stderr.write(_USAGE)
        return 2

    sub = rest[0]
    if sub not in {"setup", "status", "uninstall"}:
        sys.stderr.write(f"mini-ork zed: unknown subcommand: {sub}\n")
        sys.stderr.write(_USAGE)
        return 2

    parser = _build_parser()
    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        # argparse's own --help hits this; route through our usage.
        if exc.code == 0:
            sys.stderr.write(_USAGE)
            return 0
        sys.stderr.write(_USAGE)
        return 2

    args.root = root
    args.settings_path = _settings_path()

    if sub == "setup":
        return _cmd_setup(args)
    if sub == "status":
        return _cmd_status(args)
    return _cmd_uninstall(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ.get("MINI_ORK_ROOT", "")))