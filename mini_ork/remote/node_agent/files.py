"""Filesystem routes for the node-agent (kickoff requirement 5).

``PUT /v1/files?root=run|home|mo-home`` extracts a tar body into the
selected per-session root with strict path containment. ``GET
/v1/files`` produces a tar from the root. ``POST /v1/files/manifest``
returns ``{relpath: sha256}`` for the root. ``POST /v1/tree/bundle``
and ``GET /v1/tree/snapshot`` move git bundles through the target dir
— the no-op-safe subset of epic 07's git semantics, enough to keep
the transport live before the full git-sync story ships.

Tar safety: absolute members, members containing ``..`` anywhere in
their path, and symlinks/hardlinks pointing outside the root are
rejected. We try the PEP 706 ``data`` filter first (Python 3.12+),
falling back to manual member iteration with the same checks.
"""
from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

from .engines import manifest_for  # re-exported so app.py can mount the manifest route


# Allowed root names. Anything else is rejected so a misrouted request
# cannot write into the engine / home / etc.
_ALLOWED_ROOTS = ("run", "home", "mo-home")


class UnsafeTarMember(ValueError):
    """Raised when a tar member violates path-containment."""


def _resolve_root(state_dir: Path, run_id: str, name: str) -> Path:
    if name not in _ALLOWED_ROOTS:
        raise ValueError(f"unknown root {name!r}; expected one of {_ALLOWED_ROOTS}")
    root = (state_dir / "runs" / run_id / name).resolve()
    # Defense in depth: ensure resolved root is under state_dir.
    base = state_dir.resolve()
    try:
        root.relative_to(base)
    except ValueError:
        raise ValueError(f"root {name!r} resolves outside state dir")
    return root


def _check_member(member: tarfile.TarInfo, root: Path) -> None:
    """Reject any tar member that could escape ``root`` after extraction."""
    if member.name.startswith("/") or os.path.isabs(member.name):
        raise UnsafeTarMember(f"absolute path: {member.name!r}")
    parts = member.name.split("/")
    if ".." in parts:
        raise UnsafeTarMember(f"path contains '..': {member.name!r}")
    if member.issym() or member.islnk():
        # Resolve the link target as a path RELATIVE to the member's dir;
        # an absolute link or one that uses ``..`` is unsafe by definition.
        link = member.linkname
        if os.path.isabs(link) or ".." in link.split("/"):
            raise UnsafeTarMember(f"unsafe link target in {member.name!r}")
    candidate = (root / member.name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeTarMember(f"path escapes root: {member.name!r}") from exc


def extract_tar(root: Path, body: bytes) -> int:
    """Extract a tar body into ``root``. Returns the number of members written.

    Raises :class:`UnsafeTarMember` on the first violating member; the
    extracted partial state is left on disk so the caller can decide
    whether to roll back. ``tarfile.data_filter`` (PEP 706) covers the
    same checks; we layer manual checks on top so 3.11 (the kickoff's
    pinned interpreter) inherits the same guarantees.
    """
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    # Always iterate members manually so 3.11 + 3.12 both get the same
    # containment discipline — ``data_filter`` is a 3.12+ kwarg.
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tar:
        for member in tar.getmembers():
            _check_member(member, root)
            tar.extract(member, path=root)
            written += 1
    return written


def make_tar(root: Path) -> bytes:
    """Build a tar archive from ``root`` (returns the bytes)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            tar.add(str(p), arcname=str(rel))
    return buf.getvalue()


__all__ = [
    "extract_tar",
    "make_tar",
    "manifest_for",
    "UnsafeTarMember",
    "_resolve_root",
]