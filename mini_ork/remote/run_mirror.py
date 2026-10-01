"""Run-dir mirror between local ``$MINI_ORK_RUN_DIR`` and ``/workspace/run``.

The mirror is boundary-time sync: pushed BEFORE a remote ``spawn``/``exec``
and pulled AFTER. It does NOT stream in-progress output (epic 09 owns the
live tee). The control plane wins on a both-sides change; over-cap files
are skipped with an event and never truncated.

Module-level constants are the operator contract:

* :data:`DEFAULT_EXCLUDES` — files the mirror never pushes (executor-owned
  or huge; ``state.db*`` is the control plane's only DB writer per D5;
  ``agent-*.live.jsonl`` is produced by epic 09 — ahead of time so we never
  need a coordination step).
* :data:`DENY_LIST` — files that must never appear at ``/workspace/mo-home``
  (state, secrets, env). Negative-only; matched on basename so a subdir
  cannot bypass.
* :data:`DEFAULT_MAX_FILE_MB`, :data:`DEFAULT_MAX_TOTAL_MB` — size caps
  applied at push time. Over-cap files emit ``remote.mirror.skipped``.

Pure functions only — no HTTP, no FastAPI, no env read at module top. The
``RemoteWorkspace`` adapter (``mini_ork.runtime.backends.remote``) calls
these helpers and adds the transport glue.

Symlink semantics: the local walker FOLLOWS symlinks-to-files (a symlink
whose target is a file is hashed by content). This aligns with the remote
walker at ``mini_ork.remote.node_agent.engines.manifest_for`` which uses
``p.is_file()`` (True for symlinks-to-files). An asymmetry would create
spurious "remote-only" diffs on every push. The run dir is operator-owned
so symlink-as-attacker-primitive is not a threat surface here.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Union


# ---------------------------------------------------------------------------
# Operator contract (importable by tests without instantiating anything).
# ---------------------------------------------------------------------------

# Per kickoff §1. ``state.db*`` matches the SQLite WAL/SHM siblings the
# control plane writes during a run (D5). The glob lives at module top so
# the unit-test ``test_operator_contract_constants_are_present`` asserts it
# without reaching into a private field.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "execute.log",
    "*.pid",
    ".stop-requested",
    ".workspace-session.json",
    "state.db*",
    "agent-*.live.jsonl",  # epic 09 — pre-skipped, ahead of time
    ".mo-run-mirror.json",  # this module's own sidecar
)

# Per kickoff §2. Deny-list is a basename glob; a subdir cannot bypass it.
DENY_LIST: tuple[str, ...] = (
    "state.db",
    "state.db-*",
    "secrets.local.sh",
    "secrets*",
    "auth-tokens.txt",
    "*.env",
)

DEFAULT_MAX_FILE_MB: int = 50
DEFAULT_MAX_TOTAL_MB: int = 500

# Sidecar name under the run root. Atomic-writeable so a mid-run crash
# leaves the prior valid snapshot on disk.
_SIDECAR_NAME = ".mo-run-mirror.json"


# ---------------------------------------------------------------------------
# Manifest walker.
# ---------------------------------------------------------------------------


def file_sha256(path: Union[str, Path], *, chunk: int = 1 << 16) -> str:
    """SHA-256 over a file."""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def manifest(
    root: Union[str, Path],
    *,
    excludes: tuple[str, ...] = (),
) -> dict[str, tuple[str, int, float]]:
    """Walk ``root`` and return ``{relpath: (sha256, size, mtime)}``.

    Symlinks-to-files are followed (their target's content is hashed); this
    aligns with the remote walker at ``mini_ork.remote.node_agent.engines
    .manifest_for`` so push/pull diffs are clean. Excluded basenames are
    skipped BEFORE stat so a 50 GB log cannot poison the walk.
    Forward-slash relpaths so the local diff matches the remote manifest
    endpoint output verbatim.
    """
    root = Path(root)
    out: dict[str, tuple[str, int, float]] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        # ``is_file()`` is True for symlinks-to-files (follows), matching
        # the remote walker at ``engines.py:148-154``. Skip directories,
        # broken symlinks, sockets, and FIFOs.
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if _is_excluded(rel, excludes):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out[rel] = (file_sha256(p), int(st.st_size), float(st.st_mtime))
    return out


def diff_manifests(
    before: dict[str, str],
    after: dict[str, str],
) -> dict[str, str]:
    """``{relpath: new_sha}`` for files that changed (or appeared) between
    ``before`` and ``after``. Both args are ``{relpath: sha256}``.
    """
    out: dict[str, str] = {}
    for rp, sha in after.items():
        if before.get(rp) != sha:
            out[rp] = sha
    return out


# ---------------------------------------------------------------------------
# Push (local -> remote).
# ---------------------------------------------------------------------------


def build_push_tar(
    root: Union[str, Path],
    members: dict[str, tuple[str, int, float]],
    *,
    max_file_bytes: int,
    max_total_bytes: int,
    on_skip: Callable[[str, int], None] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Tar only the relpaths in ``members`` that fit the size caps.

    Returns ``(tar_bytes, stats)``. Files over ``max_file_bytes`` are
    skipped — never read into memory, never truncated. The total budget
    is enforced against the sum of bytes actually written.

    Members whose source file vanished between manifest + tar are silently
    dropped (the agent-side ``after`` manifest already covers that race).
    The ``on_skip(relpath, size)`` callback fires once per over-cap file
    so the caller can emit ``remote.mirror.skipped``.
    """
    root = Path(root)
    buf = io.BytesIO()
    files_in_tar = 0
    bytes_in_tar = 0
    skipped_count = 0
    start = time.time()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel, info in members.items():
            size = info[1]
            mtime = info[2]
            if size > max_file_bytes:
                skipped_count += 1
                if on_skip is not None:
                    on_skip(rel, size)
                continue
            if bytes_in_tar + size > max_total_bytes:
                skipped_count += 1
                if on_skip is not None:
                    on_skip(rel, size)
                continue
            src = root / rel
            if not src.is_file():
                continue
            data = src.read_bytes()
            ti = tarfile.TarInfo(name=rel)
            ti.size = len(data)
            ti.mtime = int(mtime)
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
            files_in_tar += 1
            bytes_in_tar += len(data)
    elapsed_ms = int((time.time() - start) * 1000)
    return buf.getvalue(), {
        "files": files_in_tar,
        "bytes": bytes_in_tar,
        "skipped": skipped_count,
        "ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Pull (remote -> local).
# ---------------------------------------------------------------------------


def apply_pull_tar(
    root: Union[str, Path],
    tar_bytes: bytes,
    changed: dict[str, str],
    local_at_push: dict[str, str],
    local_now: dict[str, str],
    *,
    on_conflict: Callable[[str], None] | None = None,
) -> tuple[list[str], int, int]:
    """Extract the pull tar onto ``root``, resolving conflicts.

    For each relpath in the tar:

    * if ``local_now[relpath]`` differs from ``local_at_push[relpath]`` ->
      local copy wins, emit ``on_conflict(relpath)``, do not write
    * else -> atomic write (temp + rename), preserving a current mtime so
      the executor's "agent wins by mtime" reader
      (``cli/execute_handlers.py:499-504``) keeps its meaning
    * relpaths in the tar that are NOT in ``changed`` are skipped (the
      remote didn't actually change them; rewrite would clobber the
      executor's mtime marker)

    Returns ``(conflicts, written_count, bytes_written)``.
    """
    root = Path(root)
    conflicts: list[str] = []
    written = 0
    bytes_written = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            rel = member.name
            if rel not in changed:
                continue
            local_sha_before = local_now.get(rel)
            pushed_sha = local_at_push.get(rel)
            if (
                local_sha_before is not None
                and pushed_sha is not None
                and local_sha_before != pushed_sha
            ):
                # Executor / a sibling process changed it during spawn.
                # Local wins; do not write.
                conflicts.append(rel)
                if on_conflict is not None:
                    on_conflict(rel)
                continue
            data = tf.extractfile(member)
            if data is None:
                continue
            payload = data.read()
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic temp + rename. Set the file's mtime to NOW so the
            # executor's mtime-greater-than-marker logic prefers the
            # freshly-mirrored agent output.
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent),
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                os.replace(tmp_path, target)
                os.utime(target, None)  # current mtime on both atime + mtime
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            written += 1
            bytes_written += len(payload)
    return conflicts, written, bytes_written


# ---------------------------------------------------------------------------
# mo-home subset (deny-list filter).
# ---------------------------------------------------------------------------


def mo_home_files(
    home: Union[str, Path],
    *,
    deny: tuple[str, ...] = DENY_LIST,
    recipe_name: str | None = None,
) -> list[Path]:
    """Return the local paths to upload to ``/workspace/mo-home``.

    Walks ``<home>/config/*.yaml`` and, when ``recipe_name`` is given,
    ``<home>/recipes/<recipe_name>/**``. The deny-list is applied as a
    basename glob so a subdir cannot bypass it. ``state.db*``,
    ``secrets*``, ``auth-tokens.txt`` and ``*.env`` are never returned.
    """
    home = Path(home)
    out: list[Path] = []
    config_dir = home / "config"
    if config_dir.is_dir():
        for p in sorted(config_dir.glob("*.yaml")):
            if _is_denied(p.name, deny):
                continue
            out.append(p)
    if recipe_name:
        recipe_dir = home / "recipes" / recipe_name
        if recipe_dir.is_dir():
            for p in sorted(recipe_dir.rglob("*")):
                if not p.is_file():
                    continue
                if _is_denied(p.name, deny):
                    continue
                out.append(p)
    return out


def build_mo_home_tar(
    home: Union[str, Path],
    files: list[Path],
) -> bytes:
    """Tar the listed ``files`` with relpath ``config/<name>`` or
    ``recipes/<recipe>/<rel>``. The receiver extracts under the
    ``mo-home`` root.
    """
    home = Path(home)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for p in files:
            try:
                rel = p.relative_to(home).as_posix()
            except ValueError:
                continue
            data = p.read_bytes()
            ti = tarfile.TarInfo(name=rel)
            ti.size = len(data)
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Sidecar (resume support).
# ---------------------------------------------------------------------------


def write_sidecar(run_dir: Union[str, Path], snap: dict[str, str]) -> None:
    """Atomic write of the push-time snapshot at ``<run_dir>/.mo-run-mirror.json``."""
    run_dir = Path(run_dir)
    sidecar = run_dir / _SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"local_at_push": snap}, sort_keys=True).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".mo-run-mirror.", suffix=".tmp", dir=str(run_dir))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, sidecar)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_sidecar(run_dir: Union[str, Path]) -> dict[str, str]:
    """Return the prior push-time snapshot, or ``{}`` if absent / corrupt."""
    run_dir = Path(run_dir)
    sidecar = run_dir / _SIDECAR_NAME
    if not sidecar.is_file():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        snap = payload.get("local_at_push", {})
        if not isinstance(snap, dict):
            return {}
        return {str(k): str(v) for k, v in snap.items()}
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _is_excluded(relpath: str, excludes: tuple[str, ...]) -> bool:
    """Match the basename against each exclude glob; first match wins."""
    if not excludes:
        return False
    base = relpath.rsplit("/", 1)[-1]
    for pat in excludes:
        if fnmatch.fnmatch(base, pat):
            return True
    return False


def _is_denied(basename: str, deny: tuple[str, ...]) -> bool:
    if not deny:
        return False
    for pat in deny:
        if fnmatch.fnmatch(basename, pat):
            return True
    return False


__all__ = [
    "DEFAULT_EXCLUDES",
    "DEFAULT_MAX_FILE_MB",
    "DEFAULT_MAX_TOTAL_MB",
    "DENY_LIST",
    "apply_pull_tar",
    "build_mo_home_tar",
    "build_push_tar",
    "diff_manifests",
    "file_sha256",
    "manifest",
    "mo_home_files",
    "read_sidecar",
    "write_sidecar",
]
