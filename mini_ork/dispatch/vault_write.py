"""The vault's write side: one name → one value, and only into the file tier.

``vault.py`` is the reader every credential path in mini-ork already shares —
the PM adapters, the MCP headers, ``env.yaml``, node boundaries — and it is
deliberately read-only: a ``set()`` growing on it would give one secret two
policies, one per caller. This module is the write side, and it writes exactly
one place: the owner-only ``secrets.local.sh`` that ``FileBackend`` reads.

Every other backend refuses, and each refusal is its own exception type:

* ``sops`` / ``openbao`` / ``command`` — a store whose *write* form this
  project has not tested. The refusal carries the argv an operator should run
  instead, so "we do not know how" is a command rather than a dead end.
* ``env`` — an environment variable belongs to the shell that spawned the
  process; there is nothing here to write.
* an unknown type — named, with the known set.

**Fail closed.** A declared secure-tier backend is never bypassed. If the chain
puts sops or OpenBao in front of the file store, ``set_secret`` refuses instead
of dropping a plaintext file where the reader would never look for it. The chain
comes from ``config/vault.yaml`` and is never guessed.

Nothing here echoes, logs or raises with a value: every message names the key,
the backend and the store path. The file tier's own rules — a shell-identifier
name, a single-line value, ``shlex.quote``, the 0600 temp file and
``os.replace`` — are :func:`mini_ork.dispatch.secrets.write_secret_exports`,
imported rather than re-implemented, so that file keeps exactly one writer.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path

from .secrets import SecretStoreError, secret_store_path, write_secret_exports
from .vault import (
    CommandBackend,
    EnvBackend,
    FileBackend,
    OpenBaoBackend,
    SopsBackend,
    VaultConfig,
    load_vault_config,
    vault_config_path,
)

#: What an operator replaces with the value in a printed command. Deliberately
#: never a value: the point of the refusal is that mini-ork never holds it.
VALUE_PLACEHOLDER = "<value>"

#: The backend types ``config/vault.yaml`` accepts, for an unknown-type message.
KNOWN_BACKENDS = ("env", "file", "sops", "command", "openbao")


class VaultWriteRefused(SecretStoreError):
    """This backend is not written through. Names the backend, never a value.

    A ``SecretStoreError`` subclass on purpose: every caller that already
    handles an unusable vault at a ``${NAME}`` boundary handles a refusal the
    same way, without learning a second exception type. ``argv`` is the command
    an operator should run instead, or ``()`` when there is none.
    """

    #: The backend type name, matching ``config/vault.yaml``'s vocabulary.
    backend = "unknown"

    def __init__(
        self, key: str, *, detail: str, argv: Sequence[str] = ()
    ) -> None:
        self.key = str(key)
        self.detail = str(detail)
        self.argv = tuple(str(part) for part in argv)
        message = f"{self.backend}: cannot store {self.key!r} — {self.detail}"
        if self.argv:
            message += f"; run: {_render_argv(self.argv)}"
        super().__init__(message)


class SopsWriteRefused(VaultWriteRefused):
    """A SOPS document is written by ``sops``, not by mini-ork."""

    backend = "sops"


class OpenBaoWriteRefused(VaultWriteRefused):
    """An OpenBao KV path is written by the ``bao`` CLI, under the server's policy."""

    backend = "openbao"


class CommandWriteRefused(VaultWriteRefused):
    """A ``command`` backend is a lookup tool; its store is that tool's to write."""

    backend = "command"


class EnvWriteRefused(VaultWriteRefused):
    """An environment variable is the operator's to set."""

    backend = "env"


class UnknownBackendWriteRefused(VaultWriteRefused):
    """A backend type this module has no writer for."""

    backend = "unknown"


def _render_argv(argv: Sequence[str]) -> str:
    """One command line, quoted the way a shell would need it.

    This is display quoting for an argv, not the value quoting the file tier
    needs (that lives in ``secrets.write_secret_exports``). Every element is a
    literal this module built — a tool name, a key name, a path, and
    ``VALUE_PLACEHOLDER`` — so no secret can reach this string.
    """
    return shlex.join([str(part) for part in argv])


def _attr(backend: object, name: str, default):
    """A backend's own detail for a refusal message, or ``default``.

    Only ever read to describe where a value would have gone: a refusal that
    cannot name the operator's file, mount or tool is a refusal that sends them
    looking for it.
    """
    return getattr(backend, name, default)


def _refusal_for(backend: object, key: str) -> VaultWriteRefused | None:
    """The refusal this backend owes ``key``, or ``None`` when it is not one."""
    if isinstance(backend, SopsBackend):
        path = str(_attr(backend, "_path", "<file>"))
        return SopsWriteRefused(
            key,
            detail=(
                f"a SOPS document ({path}) is encrypted by sops, and this "
                "project does not write one"
            ),
            argv=["sops", "set", path, f'["{key}"]', VALUE_PLACEHOLDER],
        )
    if isinstance(backend, OpenBaoBackend):
        mount = str(_attr(backend, "_mount", "secret"))
        paths = list(_attr(backend, "_paths", []) or [])
        location = str(paths[0]) if paths else "<path>"
        address = str(_attr(backend, "_address", "") or "")
        return OpenBaoWriteRefused(
            key,
            detail=(
                f"OpenBao at {address or '<address>'} is written by the bao CLI, "
                "under the server's own policy — not by mini-ork"
            ),
            argv=[
                "bao",
                "kv",
                "put",
                f"-mount={mount}",
                location,
                f"{key}={VALUE_PLACEHOLDER}",
            ],
        )
    if isinstance(backend, CommandBackend):
        argv = [str(part) for part in _attr(backend, "_argv", []) or []]
        tool = argv[0] if argv else "<command>"
        # No argv: the configured command *reads* a value, so offering it under
        # "run this instead" would invite the operator to run a lookup as if it
        # were the write. The tool's name is the actionable part, and it is in
        # the detail.
        return CommandWriteRefused(
            key,
            detail=(
                f"values come from {tool!r}, which owns its own store; write "
                f"{key!r} through that tool"
            ),
        )
    if isinstance(backend, EnvBackend):
        return EnvWriteRefused(
            key,
            detail="an environment variable is the operator's to set",
            argv=["export", f"{key}={VALUE_PLACEHOLDER}"],
        )
    return None


def _config(environment: Mapping[str, str], config: VaultConfig | None) -> VaultConfig:
    return load_vault_config(environment) if config is None else config


def write_target(config: VaultConfig, key: str) -> FileBackend:
    """The backend a write for ``key`` goes to. Raises the refusal when none can.

    The chain is walked in precedence order and the first backend that could
    *hold* a value decides. ``env`` is skipped rather than refused: it is
    ambient — always present, nothing to write, nothing to protect — so
    treating it as the answer would refuse the default ``env, file`` chain,
    which is the whole of the simple tier. Everything after it is a real store,
    and the first one wins: ``env, file`` writes the file, while ``sops, file``
    refuses rather than dropping a plaintext file behind the encrypted one.
    """
    for backend in config.backends:
        if isinstance(backend, EnvBackend):
            continue
        if isinstance(backend, FileBackend):
            return backend
        refusal = _refusal_for(backend, key)
        if refusal is not None:
            raise refusal
        name = str(getattr(backend, "name", "?"))
        raise UnknownBackendWriteRefused(
            key,
            detail=(
                f"no writer for the {name!r} backend; known: "
                f"{', '.join(KNOWN_BACKENDS)}"
            ),
        )
    raise EnvWriteRefused(
        key,
        detail=(
            "the configured chain is only the environment, so there is no store "
            "to write"
        ),
        argv=["export", f"{key}={VALUE_PLACEHOLDER}"],
    )


def _store_path(backend: FileBackend, environment: Mapping[str, str]) -> Path:
    """Where the file tier keeps its values.

    A ``vault.yaml`` that names a ``file:`` gets that path — ``FileBackend``
    resolved it against the config directory and reports it as
    ``status().source``. With no ``vault.yaml`` the chain is the built-in
    default and the path comes from ``MINI_ORK_HOME`` read from
    ``environment``, not from the process, so a caller that pinned a home gets
    that home.
    """
    if vault_config_path(environment).is_file():
        source = getattr(backend.status(), "source", "") or ""
        if source:
            return Path(source)
    return secret_store_path(environment)


def store_path(
    name: str, *, env: Mapping[str, str] | None = None, config: VaultConfig | None = None
) -> Path:
    """Where a ``set_secret(name, …)`` would write. Raises the same refusals.

    Read-only twin of :func:`set_secret`, for a caller that wants to report the
    path (or the refusal) before anything is written.
    """
    environment = os.environ if env is None else env
    return _store_path(write_target(_config(environment, config), name), environment)


def assert_writable(
    name: str, *, env: Mapping[str, str] | None = None, config: VaultConfig | None = None
) -> None:
    """Raise the refusal a ``set_secret`` for ``name`` would hit. Writes nothing.

    The add flow calls this *before* it asks anyone for a value: an operator
    whose vault is sops learns the exact command to run instead, and never
    types a secret into a flow that was going to refuse it. Returns ``None``
    when the write would go through.
    """
    environment = os.environ if env is None else env
    write_target(_config(environment, config), name)


def set_secret(
    name: str,
    value: str,
    *,
    env: Mapping[str, str] | None = None,
    config: VaultConfig | None = None,
) -> Path:
    """Store one value in the tier the vault config allows. Returns the store path.

    The file tier is the only thing written, and it is written by
    :func:`mini_ork.dispatch.secrets.write_secret_exports` — the module that
    already owns the identifier rule, the single-line rule, ``shlex.quote``,
    the 0600 temp file and the ``os.replace``. This function adds the dispatch
    and the refusals, not a second atomic writer.

    Raises :class:`VaultWriteRefused` when the configured tier is not the file
    store, and :class:`mini_ork.dispatch.secrets.SecretStoreError` when the
    file tier rejects the name or the value.
    """
    environment = os.environ if env is None else env
    resolved = _config(environment, config)
    target = write_target(resolved, name)
    return write_secret_exports({name: value}, _store_path(target, environment))


__all__ = [
    "KNOWN_BACKENDS",
    "VALUE_PLACEHOLDER",
    "CommandWriteRefused",
    "EnvWriteRefused",
    "OpenBaoWriteRefused",
    "SopsWriteRefused",
    "UnknownBackendWriteRefused",
    "VaultWriteRefused",
    "assert_writable",
    "set_secret",
    "store_path",
    "write_target",
]
