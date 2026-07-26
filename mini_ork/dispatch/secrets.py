"""Strict local secret-store support for provider credentials.

The Bash dispatcher historically sourced ``secrets.local.sh`` directly. Native
Python dispatch must not execute an arbitrary local shell file, so this module
accepts only simple ``export NAME=value`` records and validates the file before
reading it. The public configurator writes this exact format.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path


class SecretStoreError(ValueError):
    """The local credential store cannot be safely read or updated."""


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_INERT_SHELL_LINES = {"set -a", "set +a"}


def secret_store_path(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve the one per-project credential file without reading it."""
    env = os.environ if environment is None else environment
    explicit = str(env.get("MINI_ORK_SECRETS") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = Path(env.get("MINI_ORK_HOME") or ".mini-ork")
    return home / "config" / "secrets.local.sh"


def _validate_existing_store(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SecretStoreError(f"cannot inspect secret store {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretStoreError(f"secret store must be a regular file: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise SecretStoreError(f"secret store must be owned by the current user: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SecretStoreError(f"secret store permissions must be owner-only (0600): {path}")


def _parse_export_line(line: str, *, line_number: int, path: Path) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped in _INERT_SHELL_LINES:
        return None
    try:
        tokens = shlex.split(stripped, comments=True, posix=True)
    except ValueError as exc:
        raise SecretStoreError(f"invalid quoting in secret store {path}:{line_number}") from exc
    if len(tokens) == 2 and tokens[0] == "export":
        assignment = tokens[1]
    elif len(tokens) == 1:
        assignment = tokens[0]
    else:
        raise SecretStoreError(
            f"unsupported line in secret store {path}:{line_number}; use export NAME=value"
        )
    if "=" not in assignment:
        raise SecretStoreError(
            f"unsupported line in secret store {path}:{line_number}; use export NAME=value"
        )
    name, value = assignment.split("=", 1)
    if not _NAME.fullmatch(name):
        raise SecretStoreError(f"invalid variable name in secret store {path}:{line_number}")
    return name, value


def read_secret_exports(path: str | Path | None = None) -> dict[str, str]:
    """Read owner-only local exports without executing shell code.

    A missing file is a valid unconfigured state. Invalid or permissive files
    fail closed so a credential cannot be read through a symlink or a
    group/world-readable file.
    """
    store = Path(path) if path is not None else secret_store_path()
    if store.is_symlink():
        raise SecretStoreError(f"secret store must not be a symlink: {store}")
    if not store.exists():
        return {}
    _validate_existing_store(store)
    try:
        lines = store.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SecretStoreError(f"cannot read secret store {store}: {exc}") from exc
    exports: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        parsed = _parse_export_line(line, line_number=number, path=store)
        if parsed is not None:
            exports[parsed[0]] = parsed[1]
    return exports


def write_secret_exports(updates: Mapping[str, str], path: str | Path | None = None) -> Path:
    """Atomically update managed exports without writing values to stdout/stderr."""
    if not updates:
        return Path(path) if path is not None else secret_store_path()
    normalized: dict[str, str] = {}
    for name, value in updates.items():
        if not _NAME.fullmatch(str(name)):
            raise SecretStoreError(f"invalid secret variable name: {name!r}")
        text = str(value)
        if "\x00" in text or "\n" in text or "\r" in text:
            raise SecretStoreError(f"secret value for {name} must be a single line")
        normalized[str(name)] = text

    store = Path(path) if path is not None else secret_store_path()
    existing_lines: list[str] = []
    if store.is_symlink():
        raise SecretStoreError(f"secret store must not be a symlink: {store}")
    if store.exists():
        _validate_existing_store(store)
        # Validate every retained line too: the writer must never preserve a
        # shell construct the reader would later reject.
        read_secret_exports(store)
        existing_lines = store.read_text(encoding="utf-8").splitlines()

    store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(store.parent, 0o700)
    except OSError:
        pass

    remaining = dict(normalized)
    rendered: list[str] = []
    for line in existing_lines:
        match = _ASSIGNMENT.match(line.strip())
        name = match.group(1) if match else ""
        if name in remaining:
            rendered.append(f"export {name}={shlex.quote(remaining.pop(name))}")
        else:
            rendered.append(line)
    if not existing_lines:
        rendered.extend(
            [
                "# Managed by `mini-ork providers configure`.",
                "# Keep this file owner-only. It is ignored by Git.",
                "",
            ]
        )
    rendered.extend(f"export {name}={shlex.quote(value)}" for name, value in remaining.items())
    content = "\n".join(rendered).rstrip() + "\n"

    fd, temporary = tempfile.mkstemp(prefix=".secrets-", dir=store.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, store)
        os.chmod(store, 0o600)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise SecretStoreError(f"cannot write secret store {store}: {exc}") from exc
    return store
