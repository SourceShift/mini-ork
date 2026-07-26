"""Create MiniOrk's managed virtual environment and install its full Python runtime.

This module intentionally imports only the standard library: it is the first
piece of MiniOrk that runs on a clean machine, before PyYAML or FastAPI exist.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


MIN_PYTHON = (3, 11)
SYSTEM_TOOLS = ("bash", "sqlite3", "jq", "yq", "git", "curl")
RUNTIME_POINTER = Path(".mini-ork") / "runtime-python"


class InstallError(RuntimeError):
    """A prerequisite or child-command failure during a full installation."""


def venv_python(venv: Path, *, windows: bool | None = None) -> Path:
    """Return the interpreter path created by :mod:`venv` on a platform."""
    use_windows = os.name == "nt" if windows is None else windows
    return venv / ("Scripts/python.exe" if use_windows else "bin/python")


def _supports_python(command: str) -> bool:
    try:
        result = subprocess.run(
            [command, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def configure_windows_git_bash(
    *, platform_name: str = os.name, environment: dict[str, str] | None = None
) -> Path | None:
    """Expose Git-for-Windows Bash to this process when it is not already on PATH."""
    environment = os.environ if environment is None else environment
    if platform_name != "nt" or shutil.which("bash", path=environment.get("PATH")):
        return None
    roots = tuple(
        Path(value)
        for value in (environment.get("ProgramW6432"), environment.get("ProgramFiles"))
        if value
    )
    for candidate in (root / "Git" / "bin" / "bash.exe" for root in roots):
        if candidate.is_file():
            environment["PATH"] = str(candidate.parent) + os.pathsep + environment.get("PATH", "")
            return candidate
    return None


def ensure_supported_python() -> None:
    """Re-exec under a Python 3.11+ candidate when the bootstrap was older."""
    if sys.version_info >= MIN_PYTHON:
        return
    for candidate in ("python3.15", "python3.14", "python3.13", "python3.12", "python3.11"):
        path = shutil.which(candidate)
        if path and _supports_python(path):
            os.execv(path, [path, str(Path(__file__).resolve()), *sys.argv[1:]])
    raise InstallError(
        "Python 3.11+ is required. Re-run make install PYTHON=python3.11 after installing it, "
        "or use the platform package manager through make install-system-deps."
    )


def missing_system_tools() -> tuple[str, ...]:
    missing = [tool for tool in SYSTEM_TOOLS if shutil.which(tool) is None]
    bash = shutil.which("bash")
    if bash:
        try:
            result = subprocess.run([bash, "-c", "printf %s \"${BASH_VERSINFO[0]:-0}\""], capture_output=True, text=True)
            if result.returncode != 0 or int(result.stdout or "0") < 4:
                missing.append("bash>=4")
        except (OSError, ValueError):
            missing.append("bash>=4")
    return tuple(dict.fromkeys(missing))


def installation_commands(root: Path, venv: Path, install_args: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """Return the ordered, replayable commands needed for a complete install."""
    python = str(venv_python(venv))
    return (
        (sys.executable, "-m", "venv", str(venv)),
        (python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"),
        (python, "-m", "pip", "install", "--quiet", "--editable", ".[full]"),
        (python, str(root / "bin" / "mini-ork"), "install", *install_args),
        (python, str(root / "bin" / "mini-ork"), "doctor"),
    )


def write_runtime_pointer(root: Path, venv: Path, *, dry_run: bool) -> Path:
    """Persist the interpreter selected by this install for the public launcher."""
    pointer = root / RUNTIME_POINTER
    python = venv_python(venv).resolve()
    print(f"+ write {pointer} -> {python}")
    if dry_run:
        return pointer
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_name(f".{pointer.name}-{os.getpid()}")
    try:
        temporary.write_text(str(python) + "\n", encoding="utf-8")
        os.replace(temporary, pointer)
    finally:
        if temporary.exists():
            temporary.unlink()
    return pointer


def _run(command: Sequence[str], *, root: Path, dry_run: bool) -> None:
    rendered = " ".join(str(part) for part in command)
    print(f"+ {rendered}")
    if dry_run:
        return
    try:
        subprocess.run(command, cwd=root, check=True)
    except OSError as exc:
        raise InstallError(f"cannot run {command[0]}: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise InstallError(f"command failed with exit {exc.returncode}: {rendered}") from exc


def install(root: Path, venv: Path, *, install_args: Sequence[str], dry_run: bool) -> None:
    configure_windows_git_bash()
    missing = missing_system_tools()
    if missing:
        raise InstallError(
            "missing system prerequisites: " + ", ".join(missing) + ". Run make install-system-deps first."
        )
    commands = installation_commands(root, venv, install_args)
    for command in commands[:3]:
        _run(command, root=root, dry_run=dry_run)
    write_runtime_pointer(root, venv, dry_run=dry_run)
    for command in commands[3:]:
        _run(command, root=root, dry_run=dry_run)
    if not dry_run:
        print(f"✓ MiniOrk full runtime installed in {venv}")
        print("  Configure provider lanes with: mini-ork providers status <lane>")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install MiniOrk's complete local runtime")
    parser.add_argument("--venv", default=".venv", help="virtual environment path relative to the checkout")
    parser.add_argument("--dry-run", action="store_true", help="print commands without creating files or installing packages")
    parser.add_argument("--no-path", action="store_true", help="do not update the user PATH while installing mini-ork")
    parser.add_argument("--bin-dir", help="pass a custom command directory to mini-ork install")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        ensure_supported_python()
        args = parse_args(argv)
        root = Path(__file__).resolve().parents[1]
        configured_venv = Path(args.venv).expanduser()
        venv = configured_venv if configured_venv.is_absolute() else root / configured_venv
        install_args: list[str] = []
        if args.no_path:
            install_args.append("--no-path")
        if args.bin_dir:
            install_args.extend(("--bin-dir", args.bin_dir))
        install(root, venv.resolve(), install_args=install_args, dry_run=args.dry_run)
        return 0
    except InstallError as exc:
        print(f"mini-ork full install: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
