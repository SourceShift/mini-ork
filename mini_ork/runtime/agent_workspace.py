"""Opt-in resolver: pick the ``Workspace`` a node's tool-exec runs in.

Pairs with :mod:`mini_ork.runtime.run_drive`. That module decides *which host
directory* is the run's shared drive; this one decides *which environment* each
agent executes in and bind-mounts that same drive into it. Together they answer
the whole ask — "each agent in its own environment, all sharing one directory":

    MO_SHARED_DRIVE_BACKEND=local-bind   # -> resolve_run_drive_cwd picks the dir
    MO_SANDBOX_BACKEND=docker            # -> each agent gets its own container
    MO_SANDBOX_IMAGE=alpine:latest       #    bind-mounting that dir at /workspace

Contract (opt-in, loud on error):
  * ``MO_SANDBOX_BACKEND`` unset/empty → ``(None, node_cwd)``: the caller runs
    ``mo_runtime_exec`` on the host exactly as today (dead-code proof; the
    default path never touches a Workspace).
  * ``local`` → a :class:`LocalWorkspace` (which itself delegates to
    ``mo_runtime_exec``) → observable parity with the unset path.
  * ``docker`` → a per-node :class:`DockerWorkspace` bind-mounting the shared
    drive at ``/workspace``; ``exec_cwd`` is the in-container mount path, not the
    host path. The docker backend module is imported *here, lazily*, so the
    default path never imports it.
  * unknown backend → ``ValueError`` from :func:`get_workspace` — never a silent
    host fallback.

Pure stdlib + the runtime registries; no import-time I/O.
"""
from __future__ import annotations

import os
from typing import Mapping

from mini_ork.runtime.sandbox import Workspace, get_workspace

__all__ = [
    "ENV_BACKEND",
    "ENV_SCOPE",
    "ENV_IMAGE",
    "MOUNT_PATH",
    "sandbox_backend",
    "sandbox_scope",
    "resolve_agent_workspace",
]

ENV_BACKEND = "MO_SANDBOX_BACKEND"
ENV_SCOPE = "MO_SANDBOX_SCOPE"
ENV_IMAGE = "MO_SANDBOX_IMAGE"
ENV_DRIVE_ROOT = "MO_SHARED_DRIVE_ROOT"
MOUNT_PATH = "/workspace"


def sandbox_backend(env: Mapping[str, str] | None = None) -> str:
    """The configured sandbox backend name, or ``""`` when unset (host)."""
    src = os.environ if env is None else env
    return (src.get(ENV_BACKEND) or "").strip()


def sandbox_scope(env: Mapping[str, str] | None = None) -> str:
    """Isolation scope: ``tool`` (route tool-exec) or ``agent`` (route the CLI).

    Defaults to ``tool`` — the safe first step that only routes shell tool-exec
    into the sandbox, leaving the coding-agent CLI on the host.
    """
    src = os.environ if env is None else env
    return (src.get(ENV_SCOPE) or "tool").strip() or "tool"


def resolve_agent_workspace(
    node_cwd: str,
    *,
    env: Mapping[str, str] | None = None,
    drive_root: str | None = None,
) -> tuple[Workspace | None, str]:
    """Resolve the ``Workspace`` a node's tool-exec should run in.

    Returns ``(workspace, exec_cwd)``. ``workspace`` is ``None`` only when the
    backend is unset — the signal to keep today's exact host execution. The
    caller owns the workspace lifecycle (``up()``/``down()``).
    """
    src = os.environ if env is None else env
    backend = sandbox_backend(src)
    if not backend:
        # Dead-code path: no Workspace at all → caller uses mo_runtime_exec.
        return None, node_cwd
    if backend == "local":
        # Parity: LocalWorkspace.exec delegates straight to mo_runtime_exec.
        return get_workspace("local", root=node_cwd), node_cwd
    if backend == "docker":
        # Lazy import: the docker backend is only pulled in when opted into,
        # keeping the default path free of any daemon/CLI dependency.
        import mini_ork.runtime.backends.docker  # noqa: F401  (registers "docker")

        root = drive_root or (src.get(ENV_DRIVE_ROOT) or "").strip() or node_cwd
        image = (src.get(ENV_IMAGE) or "").strip()
        ws = get_workspace(
            "docker", image=image, drive_root=root, mount_path=MOUNT_PATH
        )
        return ws, MOUNT_PATH
    # Unknown backend → loud ValueError (never a silent host fallback).
    return get_workspace(backend, root=node_cwd), node_cwd
