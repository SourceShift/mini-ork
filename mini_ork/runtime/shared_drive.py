"""Run-shared virtual drive — the primitive every agent in a run reads/writes.

A ``SharedDrive`` is created once per run and (in a later phase) mounted at a
fixed path inside every node's ``Workspace`` so state flows node→node/agent→agent
for the whole run: the target working tree, ``.mini-ork/runs/<id>/`` artifacts,
and the artifact manifest all live on it. It is the artifact-graph's filesystem
made real and shared across sandboxes.

The default ``local-bind`` backend is a plain host directory — zero cloud
dependency, the dev-loop default. Later phases register cloud backends
(``e2b-volume``/``modal-volume``/``daytona-volume``/``juicefs``) behind the same
protocol; a provider's native Volume *is* a shared drive, so those backends map
onto this interface directly.

Design invariants:
  * **The drive is the pet; sandboxes are cattle.** Durable run state lives here,
    so ``down()`` defaults to a no-op (it never deletes the host dir) — a crashed
    teardown must not destroy the run's work. Ephemeral scratch drives opt in via
    ``ephemeral=True``.
  * **Containment.** All access is via run-relative paths resolved under the
    drive root; a path that escapes the root (``..`` traversal or an absolute
    path) raises ``ValueError`` before any file is touched. Concurrent-write
    safety across parallel nodes is the CAID ``--owns`` registry's job, layered
    on top — this module only guarantees a node cannot reach outside the drive.

This module adds NEW surface only; nothing imports it yet. Wiring the drive into
``RunContext``/``MO_TARGET_CWD`` and the artifact graph is a deliberately separate
step (it changes every run's working dir). Pure stdlib; no import-time I/O.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

__all__ = [
    "SharedDrive",
    "LocalBindDrive",
    "SharedDriveFactory",
    "register_shared_drive_backend",
    "get_shared_drive",
]

_DEFAULT_BACKEND = "local-bind"


@runtime_checkable
class SharedDrive(Protocol):
    """A virtual drive shared by every agent/node in a run.

    Paths passed to ``put``/``get``/``sub_path`` are **run-relative** (e.g.
    ``"artifacts/plan.json"``); the drive resolves them under its root and
    guarantees they stay inside it.
    """

    def mount_path(self) -> str:
        """Absolute path where the drive's contents are visible."""
        ...

    def sub_path(self, rel: str) -> str:
        """Resolve a run-relative path to its absolute path on the drive.

        Raises ``ValueError`` if ``rel`` escapes the drive root.
        """
        ...

    def put(self, rel: str, content: str) -> str:
        """Write ``content`` at run-relative ``rel``; return its absolute path."""
        ...

    def get(self, rel: str) -> str:
        """Read and return the text at run-relative ``rel``."""
        ...

    def list(self, subdir: str = "") -> list[str]:
        """List run-relative paths of files under ``subdir`` (recursive, sorted)."""
        ...

    def up(self) -> None:
        """Provision the drive (create the root)."""
        ...

    def down(self) -> None:
        """Release the drive (no-op unless the drive is ephemeral)."""
        ...


class LocalBindDrive:
    """Host-directory ``SharedDrive`` — a bind-mount is just the dir itself.

    ``root`` is the host path that stands in for the ``/workspace`` mount a cloud
    Volume would expose. ``ephemeral=True`` makes ``down()`` remove the tree
    (scratch drives); the default keeps durable run state.
    """

    def __init__(self, *, root: str, ephemeral: bool = False) -> None:
        if not root:
            raise ValueError("LocalBindDrive requires a non-empty root path")
        self._root = os.path.abspath(root)
        self._ephemeral = ephemeral

    def mount_path(self) -> str:
        return self._root

    def _resolve(self, rel: str) -> str:
        if os.path.isabs(rel):
            raise ValueError(f"shared-drive path must be relative, got {rel!r}")
        full = os.path.normpath(os.path.join(self._root, rel))
        root_real = os.path.realpath(self._root)
        full_real = os.path.realpath(full)
        if full_real != root_real and not full_real.startswith(root_real + os.sep):
            raise ValueError(f"path escapes the shared drive: {rel!r}")
        return full

    def sub_path(self, rel: str) -> str:
        self.up()
        return self._resolve(rel)

    def put(self, rel: str, content: str) -> str:
        path = self.sub_path(rel)
        os.makedirs(os.path.dirname(path) or self._root, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def get(self, rel: str) -> str:
        return Path(self._resolve(rel)).read_text(encoding="utf-8")

    def list(self, subdir: str = "") -> list[str]:
        base = self._resolve(subdir) if subdir else self._root
        if not os.path.isdir(base):
            return []
        out: list[str] = []
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                abs_path = os.path.join(dirpath, name)
                out.append(os.path.relpath(abs_path, self._root))
        return sorted(out)

    def up(self) -> None:
        os.makedirs(self._root, exist_ok=True)

    def down(self) -> None:
        # Default: keep durable state (the drive is the pet). Only an explicitly
        # ephemeral scratch drive is torn down.
        if not self._ephemeral:
            return None
        import shutil

        shutil.rmtree(self._root, ignore_errors=True)
        return None


SharedDriveFactory = Callable[..., SharedDrive]

_SHARED_DRIVE_BACKENDS: dict[str, SharedDriveFactory] = {}


def register_shared_drive_backend(name: str, factory: SharedDriveFactory) -> None:
    """Register a ``SharedDrive`` factory under ``name`` (SOLID extension seam).

    Mirrors ``register_workspace_backend`` / ``register_provider_kind``: later
    phases add ``modal-volume``/``e2b-volume`` here without editing this module.
    Last write wins, matching the other registries.
    """
    _SHARED_DRIVE_BACKENDS[name] = factory


def get_shared_drive(backend: str = _DEFAULT_BACKEND, **kwargs: Any) -> SharedDrive:
    """Resolve ``backend`` to a live ``SharedDrive``; unknown → ``ValueError``.

    ``kwargs`` pass to the factory (e.g. ``root=``/``ephemeral=`` for local-bind,
    a volume id for a future cloud backend).
    """
    factory = _SHARED_DRIVE_BACKENDS.get(backend)
    if factory is None:
        known = ", ".join(sorted(_SHARED_DRIVE_BACKENDS)) or "(none)"
        raise ValueError(
            f"unknown shared-drive backend {backend!r}; registered: {known}"
        )
    return factory(**kwargs)


# Built-in default: the host-directory drive. Registering a factory in a dict is
# the only import-time effect (no I/O, no env mutation).
register_shared_drive_backend(
    _DEFAULT_BACKEND, lambda **kw: LocalBindDrive(**kw)
)
