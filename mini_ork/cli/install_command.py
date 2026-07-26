"""Cross-platform, per-user installation for the public ``mini-ork`` command.

The launcher is deliberately a small, marked wrapper rather than a symlink.
That marker lets a later install repair a checkout that has moved without
silently replacing an unrelated executable named ``mini-ork``.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


_MANAGED_MARKER = "Managed by mini-ork"
_PROFILE_BEGIN = "# >>> mini-ork PATH >>>"
_PROFILE_END = "# <<< mini-ork PATH <<<"
_USAGE = """Usage:
  mini-ork install [--bin-dir <path>] [--no-path] [--force] [--dry-run]

Installs a user-level mini-ork launcher. On macOS, Linux, and WSL, the default
is ~/.local/bin. On Windows, it is %LOCALAPPDATA%\\mini-ork\\bin.

Options:
  --bin-dir <path>  Install the launcher in this directory instead of the default
  --no-path         Do not update the user shell profile or Windows user PATH
  --force           Replace an existing non-MiniOrk file at the target path
  --dry-run         Print planned changes without writing files
"""


class InstallError(ValueError):
    """A recoverable installation error that should be shown to the operator."""


@dataclass(frozen=True)
class InstallResult:
    target: Path
    launcher_changed: bool
    path_changed: bool
    path_location: Path | str | None
    notices: tuple[str, ...]


def _is_windows(platform_name: str | None = None) -> bool:
    return (platform_name or os.name).lower() in {"nt", "windows", "win32"}


def _home(env: Mapping[str, str], *, windows: bool) -> Path:
    value = env.get("USERPROFILE") if windows else env.get("HOME")
    value = value or env.get("HOME") or env.get("USERPROFILE")
    return Path(value).expanduser() if value else Path.home()


def default_bin_dir(env: Mapping[str, str], *, platform_name: str | None = None) -> Path:
    """Return the non-privileged bin directory for a platform."""
    windows = _is_windows(platform_name)
    configured = env.get("MINI_ORK_BIN_DIR")
    if configured:
        return Path(configured).expanduser()
    home = _home(env, windows=windows)
    if windows:
        local_app_data = env.get("LOCALAPPDATA")
        return Path(local_app_data).expanduser() / "mini-ork" / "bin" if local_app_data else home / "AppData" / "Local" / "mini-ork" / "bin"
    return home / ".local" / "bin"


def _posix_launcher(source: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"# {_MANAGED_MARKER}. Re-run `mini-ork install` after moving this checkout.\n"
        f"exec {shlex.quote(str(source))} \"$@\"\n"
    )


def _windows_launcher(source: Path) -> str:
    escaped = str(source).replace('"', '""')
    return (
        "@echo off\r\n"
        f"rem {_MANAGED_MARKER}. Re-run mini-ork install after moving this checkout.\r\n"
        f"py -3 \"{escaped}\" %*\r\n"
        "if not errorlevel 9009 exit /b %errorlevel%\r\n"
        f"python \"{escaped}\" %*\r\n"
        "exit /b %errorlevel%\r\n"
    )


def _is_managed_target(target: Path) -> bool:
    if target.is_symlink():
        # Earlier releases installed a direct symlink to the public launcher.
        # Only repair it when the resolved source identifies itself as MiniOrk;
        # an arbitrary symlink must still require explicit --force.
        try:
            source = target.resolve(strict=True)
            content = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "Public mini-ork launcher" in content or "MINI_ORK_ROOT" in content
    if not target.is_file():
        return False
    try:
        return _MANAGED_MARKER in target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _write_launcher(target: Path, content: str, *, force: bool, dry_run: bool) -> bool:
    if target.exists() or target.is_symlink():
        if not force and not _is_managed_target(target):
            raise InstallError(
                f"refusing to replace existing non-MiniOrk file: {target} (re-run with --force to replace it)"
            )
        try:
            if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == content:
                return False
        except OSError:
            pass
    if dry_run:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.mini-ork-install-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        temporary.chmod(0o755)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _path_entries(value: str, *, separator: str) -> list[str]:
    return [entry for entry in value.split(separator) if entry]


def _path_contains(value: str, directory: Path, *, windows: bool) -> bool:
    separator = ";" if windows else ":"
    wanted = os.path.normcase(str(directory.resolve(strict=False))) if windows else str(directory.resolve(strict=False))
    for entry in _path_entries(value, separator=separator):
        candidate = Path(entry).expanduser().resolve(strict=False)
        comparable = os.path.normcase(str(candidate)) if windows else str(candidate)
        if comparable == wanted:
            return True
    return False


def _profile_path(env: Mapping[str, str], home: Path) -> tuple[Path | None, str]:
    if configured := env.get("MINI_ORK_SHELL_RC"):
        return Path(configured).expanduser(), "custom"
    shell = Path(env.get("SHELL", "")).name
    if shell == "zsh":
        return home / ".zshrc", shell
    if shell == "bash":
        return home / ".bashrc", shell
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish", shell
    return None, shell or "unknown"


def _profile_block(directory: Path, shell: str) -> str:
    if shell == "fish":
        command = f"fish_add_path {shlex.quote(str(directory))}"
    else:
        command = f"export PATH={shlex.quote(str(directory))}:$PATH"
    return f"{_PROFILE_BEGIN}\n{command}\n{_PROFILE_END}\n"


def _upsert_profile(path: Path, directory: Path, *, shell: str, dry_run: bool) -> bool:
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise InstallError(f"cannot read shell profile {path}: {exc}") from exc
    block = _profile_block(directory, shell)
    start = existing.find(_PROFILE_BEGIN)
    end = existing.find(_PROFILE_END, start) if start >= 0 else -1
    if start >= 0 and end >= 0:
        end += len(_PROFILE_END)
        if existing[start:end] == block.rstrip("\n"):
            return False
        updated = existing[:start] + block.rstrip("\n") + existing[end:]
    else:
        updated = existing
        if updated and not updated.endswith("\n"):
            updated += "\n"
        updated += block
    if dry_run:
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"cannot update shell profile {path}: {exc}") from exc
    return True


def merge_windows_path(value: str, directory: Path) -> tuple[str, bool]:
    """Add ``directory`` to a semicolon-separated Windows PATH once."""
    if _path_contains(value, directory, windows=True):
        return value, False
    suffix = str(directory)
    return (f"{value};{suffix}" if value else suffix), True


def _update_windows_path(directory: Path, *, dry_run: bool) -> bool:
    if dry_run:
        return True
    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - only possible off Windows.
        raise InstallError("Windows registry support is unavailable in this Python runtime") from exc
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, _ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current = ""
            updated, changed = merge_windows_path(str(current), directory)
            if changed:
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, updated)
    except OSError as exc:
        raise InstallError(f"cannot update the Windows user PATH: {exc}") from exc
    if changed:
        try:
            import ctypes

            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0, 1000, None)
        except (AttributeError, OSError):
            pass
    return changed


def install(
    *,
    root: str | Path,
    bin_dir: str | Path | None = None,
    update_path: bool = True,
    force: bool = False,
    dry_run: bool = False,
    environment: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> InstallResult:
    """Install the public launcher and optionally make its directory discoverable."""
    env = dict(os.environ if environment is None else environment)
    root_path = Path(root).expanduser().resolve()
    source = root_path / "bin" / "mini-ork"
    if not source.is_file():
        raise InstallError(f"mini-ork launcher not found: {source}")
    windows = _is_windows(platform_name)
    target_dir = Path(bin_dir).expanduser() if bin_dir else default_bin_dir(env, platform_name=platform_name)
    target = target_dir / ("mini-ork.cmd" if windows else "mini-ork")
    content = _windows_launcher(source) if windows else _posix_launcher(source)
    launcher_changed = _write_launcher(target, content, force=force, dry_run=dry_run)
    notices: list[str] = []
    path_changed = False
    path_location: Path | str | None = None

    if not update_path:
        notices.append("PATH management skipped (--no-path).")
    elif _path_contains(env.get("PATH", ""), target_dir, windows=windows):
        notices.append(f"{target_dir} is already on PATH.")
    elif windows:
        path_changed = _update_windows_path(target_dir, dry_run=dry_run)
        path_location = "Windows user PATH"
    else:
        home = _home(env, windows=False)
        profile, shell = _profile_path(env, home)
        if profile is None:
            notices.append(
                f"could not identify a shell profile for {shell}; add {target_dir} to PATH manually or set MINI_ORK_SHELL_RC"
            )
        else:
            path_changed = _upsert_profile(profile, target_dir, shell=shell, dry_run=dry_run)
            path_location = profile

    return InstallResult(target, launcher_changed, path_changed, path_location, tuple(notices))


def _parse_args(argv: list[str]) -> tuple[Path | None, bool, bool, bool] | None:
    bin_dir: Path | None = None
    update_path = True
    force = False
    dry_run = False
    while argv:
        item = argv.pop(0)
        if item in {"--help", "-h"}:
            return None
        if item == "--bin-dir":
            if not argv:
                raise InstallError("--bin-dir requires <path>")
            bin_dir = Path(argv.pop(0))
        elif item.startswith("--bin-dir="):
            bin_dir = Path(item.split("=", 1)[1])
        elif item == "--no-path":
            update_path = False
        elif item == "--force":
            force = True
        elif item == "--dry-run":
            dry_run = True
        else:
            raise InstallError(f"unknown install option: {item}")
    return bin_dir, update_path, force, dry_run


def main(argv: list[str] | None = None, *, root: str | Path | None = None) -> int:
    try:
        parsed = _parse_args(list(sys.argv[1:] if argv is None else argv))
        if parsed is None:
            sys.stdout.write(_USAGE)
            return 0
        bin_dir, update_path, force, dry_run = parsed
        result = install(
            root=root or os.environ.get("MINI_ORK_ROOT") or Path(__file__).resolve().parents[2],
            bin_dir=bin_dir,
            update_path=update_path,
            force=force,
            dry_run=dry_run,
        )
    except InstallError as exc:
        sys.stderr.write(f"mini-ork install: {exc}\n")
        return 2

    action = "Would install" if dry_run else "Installed"
    change = "updated" if result.launcher_changed else "already current"
    print(f"{action} mini-ork launcher: {result.target} ({change})")
    if result.path_location:
        path_action = "would update" if dry_run and result.path_changed else (
            "updated" if result.path_changed else "already configured"
        )
        print(f"PATH: {path_action} {result.path_location}")
    for notice in result.notices:
        print(f"Note: {notice}")
    if result.path_changed:
        print("Open a new terminal, then run: mini-ork version")
    else:
        print("Verify with: mini-ork version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
