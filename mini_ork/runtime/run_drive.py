"""Opt-in seam that routes a run's working cwd through a ``SharedDrive``.

By default mini-ork runs on the host tree exactly as before — this module is a
no-op unless ``MO_SHARED_DRIVE_BACKEND`` is set. When it is, the implementer's
target cwd (``MO_TARGET_CWD``, resolved in ``mini_ork.cli.execute``) is redirected
onto a per-run :class:`~mini_ork.runtime.shared_drive.SharedDrive` mount, so every
node/agent in the run reads and writes the same virtual drive — the P1a drive
made load-bearing for real runs (the "shared virtual drive all agents can access"
ask).

This is deliberately the *only* wiring point: flip one env var to move a run onto
a drive, leave it unset for today's behavior. Cloud backends
(``modal-volume``/``e2b-volume``/…) register behind the same ``get_shared_drive``
seam, so pointing a run at a cloud Volume is a backend-name change, not a code
change here.

Env contract:
  * ``MO_SHARED_DRIVE_BACKEND`` — registered backend name (e.g. ``local-bind``).
    Unset/empty → disabled, ``resolve_run_drive_cwd`` returns ``default_cwd``.
  * ``MO_SHARED_DRIVE_ROOT`` — host path for the drive root; defaults to the
    run's own ``default_cwd`` when unset (local-bind stands in for the mount).

Pure stdlib + the existing ``shared_drive`` registry; no import-time I/O.
"""
from __future__ import annotations

import os
from typing import Mapping

from mini_ork.runtime.shared_drive import get_shared_drive

__all__ = [
    "ENV_BACKEND",
    "ENV_ROOT",
    "shared_drive_enabled",
    "resolve_run_drive_cwd",
]

ENV_BACKEND = "MO_SHARED_DRIVE_BACKEND"
ENV_ROOT = "MO_SHARED_DRIVE_ROOT"


def shared_drive_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True iff a run should route its cwd through a shared drive (opt-in)."""
    src = os.environ if env is None else env
    return bool((src.get(ENV_BACKEND) or "").strip())


def resolve_run_drive_cwd(
    default_cwd: str, *, env: Mapping[str, str] | None = None
) -> str:
    """Return the cwd a run should use, redirected onto a shared drive if opted in.

    Disabled (``MO_SHARED_DRIVE_BACKEND`` unset) → ``default_cwd`` unchanged, so
    the default host-tree behavior is byte-for-byte preserved. Enabled → provision
    the drive (``up()``) and return its ``mount_path()``. An unknown backend name
    propagates ``ValueError`` from ``get_shared_drive`` — fail loud, not silent.
    """
    src = os.environ if env is None else env
    backend = (src.get(ENV_BACKEND) or "").strip()
    if not backend:
        return default_cwd
    root = (src.get(ENV_ROOT) or "").strip() or default_cwd
    drive = get_shared_drive(backend, root=root)
    drive.up()
    return drive.mount_path()
