"""Backend-agnostic ``Workspace`` execution protocol (sandbox P0).

A ``Workspace`` is where a mini-ork node's commands and coding agents run.
The default ``local`` backend runs on the host and preserves today's exact
behavior — it delegates to :func:`mini_ork.runtime.contract.mo_runtime_exec`,
so cwd is pinned per call, stdout+stderr are merged, and a timeout kills the
whole process group. Later phases register cloud/sandbox backends
(``docker``/``e2b``/``modal``/``daytona``) behind the same protocol so the
call sites never branch on the backend.

This module adds NEW surface only. Nothing imports it yet; wiring
``dispatch``/``execute`` through ``Workspace.exec`` is a later phase. It
mirrors the repo's registry-extension pattern (see
``register_node_handler`` / ``register_provider_kind``): a backend registers
a factory by name and is resolved via :func:`get_workspace`.

Import-time contract: pure stdlib + ``mini_ork.runtime.contract`` only, no
third-party dependency, no I/O and no env mutation at import (registering the
built-in ``local`` factory in a dict is the only import-time effect, matching
the existing SOLID seams).
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from mini_ork.runtime.contract import mo_runtime_exec

__all__ = [
    "Workspace",
    "LocalWorkspace",
    "WorkspaceFactory",
    "register_workspace_backend",
    "get_workspace",
]


@runtime_checkable
class Workspace(Protocol):
    """Where a node's commands/agents execute — host today, sandbox later.

    The protocol is deliberately tiny so any backend (host subprocess, docker,
    a cloud microVM) can satisfy it. ``exec`` returns ``(returncode, output)``
    with stdout+stderr merged, matching the runtime contract's stream shape.
    """

    def exec(self, cmd: str, *, cwd: str, timeout: int) -> tuple[int, str]:
        """Run ``cmd`` with ``cwd`` pinned; return (returncode, merged output)."""
        ...

    def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        timeout: float,
        env: Mapping[str, str],
        cwd: str | None,
    ) -> tuple[int, str, str]:
        """Spawn a harness CLI (``argv``, no shell) with the prompt fed on
        ``stdin``; return ``(rc, stdout, stderr)`` with the two streams **kept
        separate**.

        This is the ``scope=agent`` verb — route the coding-agent CLI *itself*
        into the workspace — and it deliberately differs from ``exec`` (the
        ``scope=tool`` verb): ``exec`` is many warm calls returning ONE merged
        stream with no stdin, right for shell tool-exec; ``spawn`` is a one-shot
        with a stdin channel and separated stdout/stderr, the shape the dispatch
        spawn contract requires (a merged stream would corrupt the provider's
        JSON envelope on stdout). ``rc`` is faithful: ``124`` on timeout,
        ``127`` on spawn failure, else the process's own code. Each backend
        re-establishes the A.1 invariant in its own isolation domain (host:
        ``start_new_session``; container: no host ``/dev/tty`` + dies with the
        container)."""
        ...

    def put(self, content: str) -> str:
        """Write ``content`` to a fresh path inside the workspace; return it."""
        ...

    def get(self, path: str) -> str:
        """Read and return the text at ``path`` inside the workspace."""
        ...

    def up(self) -> None:
        """Provision the workspace (no-op for an always-on host backend)."""
        ...

    def down(self) -> None:
        """Tear the workspace down (no-op for the host backend)."""
        ...


class LocalWorkspace:
    """Host-backed ``Workspace`` — today's exact behavior, nothing new.

    ``exec`` reuses :func:`mo_runtime_exec` rather than reimplementing the
    pgid-kill timeout logic, so the ``local`` backend is byte-for-byte the
    runtime contract with the tuple flipped to ``(rc, output)``. ``put``/
    ``get`` use ordinary files under a lazily created temp dir; ``up``/``down``
    are no-ops because the host is always available.
    """

    def __init__(self, *, root: str | None = None) -> None:
        # Explicit root lets a caller pin the scratch dir (e.g. a run-local
        # path); otherwise it is allocated lazily on first ``put`` so an
        # instance that never writes leaves no temp dir behind.
        self._root: str | None = root

    def _ensure_root(self) -> str:
        if self._root is None:
            self._root = tempfile.mkdtemp(prefix="mo-workspace-")
        os.makedirs(self._root, exist_ok=True)
        return self._root

    def exec(self, cmd: str, *, cwd: str, timeout: int) -> tuple[int, str]:
        output, rc = mo_runtime_exec(cmd, cwd=cwd, timeout=timeout, backend="local")
        return rc, output

    def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        timeout: float,
        env: Mapping[str, str],
        cwd: str | None,
    ) -> tuple[int, str, str]:
        # The host spawn transport IS core.spawn_local — today's dispatch Popen
        # verbatim (TTY-severed, pgid-reaped, streams separated) — so the local
        # backend is byte-for-byte the in-process path with zero regression.
        # Imported lazily to keep this module's import-time surface stdlib-only
        # and free of any dispatch dependency (no import cycle: dispatch.core
        # imports only dispatch.models, never runtime).
        from mini_ork.dispatch.core import spawn_local

        return spawn_local(argv, stdin=stdin, timeout=timeout, env=env, cwd=cwd)

    def put(self, content: str) -> str:
        root = self._ensure_root()
        fd, path = tempfile.mkstemp(dir=root, prefix="put-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def get(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def up(self) -> None:  # no-op: the host is always provisioned
        return None

    def down(self) -> None:  # no-op: never tear the host down
        return None


WorkspaceFactory = Callable[..., Workspace]

_WORKSPACE_BACKENDS: dict[str, WorkspaceFactory] = {}


def register_workspace_backend(name: str, factory: WorkspaceFactory) -> None:
    """Register a ``Workspace`` factory under ``name`` (SOLID extension seam).

    Mirrors ``register_node_handler`` / ``register_provider_kind``: later
    phases call this at import to add ``docker``/``e2b``/``modal`` without
    editing this module. Re-registering a name overrides it (last write wins),
    matching the other registries.
    """
    _WORKSPACE_BACKENDS[name] = factory


def get_workspace(backend: str = "local", **kwargs: Any) -> Workspace:
    """Resolve ``backend`` to a live ``Workspace``; unknown → ``ValueError``.

    ``kwargs`` pass through to the factory (e.g. ``root=`` for the local
    backend, image/CPU/memory for a future cloud backend).
    """
    factory = _WORKSPACE_BACKENDS.get(backend)
    if factory is None:
        known = ", ".join(sorted(_WORKSPACE_BACKENDS)) or "(none)"
        raise ValueError(
            f"unknown workspace backend {backend!r}; registered: {known}"
        )
    return factory(**kwargs)


# Built-in default: the host backend. Registering a factory in a dict is the
# only import-time effect (no I/O, no env mutation) — the same shape the other
# registries use to install their built-ins.
register_workspace_backend("local", lambda **kw: LocalWorkspace(**kw))
