"""Leaked-sandbox reaper (sandbox P4a, the GC safety net).

Finds and force-removes mini-ork sandbox instances that outlived their run — a
run that crashes before its ``finally`` teardown leaves a labeled container (or
microVM) alive forever. This module sweeps them by **age (TTL)**, never by run
liveness: TTL keeps the reaper decoupled from ``state.db`` and the run
lifecycle, so it can never mistakenly kill work a *different* live run started.

It only ever touches instances carrying mini-ork's own markers — Docker
containers labeled ``mo.sandbox=1`` (see ``backends/docker.py``) and microVMs
named ``mo-mvm-<hex>`` (``backends/microvm.py``) — never a broad ``docker rm``.

No new pip dependency: the Docker path shells out to the ``docker`` CLI via a
single ``_run`` indirection (so tests fake it without a daemon); the microVM
path guards its SDK import and is a best-effort no-op when the SDK is absent.
Import-time is side-effect-free: importing this module runs no ``docker`` or SDK
call — it only defines pure functions.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime

__all__ = [
    "DEFAULT_MAX_AGE_S",
    "reap_docker",
    "reap_microvm",
    "reap_sandboxes",
]

# Generous default so a long-running node is never reaped mid-flight; a leaked
# container is measured in *hours* of overhang, not minutes. Override per-call
# (``--max-age``) or globally via the ``MO_SANDBOX_MAX_AGE`` env (seconds).
DEFAULT_MAX_AGE_S = 6 * 60 * 60  # 6 hours

_DOCKER_BIN = "docker"
_RUN_LABEL = "mo.sandbox=1"  # mirrors backends/docker.py::_RUN_LABEL


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a CLI command with captured streams — the single test seam.

    Every Docker invocation flows through here so a test can monkeypatch one
    function and drive the whole reaper without a real daemon. A missing binary
    surfaces as ``FileNotFoundError`` for the caller to treat as "backend
    absent" (fail-open), matching the ``docker`` backend's own contract.
    """
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_max_age(max_age_s: int | None) -> int:
    """Explicit arg wins; else ``MO_SANDBOX_MAX_AGE`` env; else the default."""
    if max_age_s is not None:
        return max_age_s
    raw = (os.environ.get("MO_SANDBOX_MAX_AGE") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_AGE_S


def _parse_created_at(raw: str) -> float | None:
    """Parse Docker's ``{{.CreatedAt}}`` into epoch seconds, else ``None``.

    Docker prints e.g. ``2026-08-03 10:15:30 +0000 UTC`` — a timezone *name*
    (``UTC``) that ``%z`` can't consume, so we keep only the first three
    space-separated fields (date, time, numeric offset) and parse those. Any
    shape we don't recognise returns ``None`` and the container is skipped
    (fail-open: an unparseable age is never treated as "old enough to reap").
    """
    parts = raw.strip().split()
    if len(parts) < 3:
        return None
    stamp = f"{parts[0]} {parts[1]} {parts[2]}"
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def _docker_alive() -> bool:
    """True iff the ``docker`` CLI is present AND its daemon answers."""
    if shutil.which(_DOCKER_BIN) is None:
        return False
    try:
        r = _run([_DOCKER_BIN, "info", "--format", "{{.ServerVersion}}"])
    except (FileNotFoundError, OSError):
        return False
    return r.returncode == 0


def reap_docker(
    *,
    max_age_s: int | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> list[str]:
    """Reap leaked ``mo.sandbox=1`` containers older than ``max_age_s``.

    Lists labeled containers with their creation time, force-removes only those
    whose age exceeds ``max_age_s`` (younger ones are left running), and returns
    the reaped container ids. ``dry_run`` selects the same set but removes
    nothing. A missing ``docker`` CLI or a dead daemon yields ``[]`` and raises
    nothing (fail-open).
    """
    max_age = _resolve_max_age(max_age_s)
    clock = time.time() if now is None else now
    if not _docker_alive():
        return []
    try:
        listing = _run(
            [
                _DOCKER_BIN,
                "ps",
                "--filter",
                f"label={_RUN_LABEL}",
                "--format",
                "{{.ID}} {{.CreatedAt}}",
            ]
        )
    except (FileNotFoundError, OSError):
        return []
    if listing.returncode != 0:
        return []

    reaped: list[str] = []
    for line in listing.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        cid, _, created_raw = line.partition(" ")
        if not cid:
            continue
        created = _parse_created_at(created_raw)
        if created is None:
            continue  # unparseable age → skip (never reap on a guess)
        if clock - created <= max_age:
            continue  # too young — this may be a live run's warm container
        if not dry_run:
            try:
                _run([_DOCKER_BIN, "rm", "-f", cid])
            except (FileNotFoundError, OSError):
                # Daemon died mid-sweep: stop touching it, report what we got.
                break
        reaped.append(cid)
    return reaped


def reap_microvm(
    *,
    max_age_s: int | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> list[str]:
    """Best-effort reaper for leaked ``mo-mvm-<hex>`` microVMs.

    The microsandbox SDK import is guarded: when it is unavailable (the common
    case on a host with no microVM backend) this returns ``[]`` and raises
    nothing, exactly like ``reap_docker`` on a host with no Docker. Signature
    and return shape mirror :func:`reap_docker` so callers treat both backends
    uniformly.
    """
    _ = (_resolve_max_age(max_age_s), dry_run, now)  # honored once a list API lands
    try:
        import microsandbox  # noqa: F401  (guarded — absence is expected)
    except Exception:
        return []
    # SDK present but the local backend exposes no age-listing API to enumerate
    # lingering microVMs (no msb server to query); reaping stays a no-op until a
    # list surface exists. Returning [] keeps this fail-open and side-effect-free.
    return []


def reap_sandboxes(
    *,
    backend: str = "all",
    max_age_s: int | None = None,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Dispatch the reap to one backend or all; return ``{backend: [ids]}``.

    ``backend`` is ``all`` (docker + microvm), ``docker``, or ``microvm``. Each
    backend is fail-open, so the result always has a key per requested backend
    even when nothing was reaped (or the backend is absent).
    """
    out: dict[str, list[str]] = {}
    if backend in ("all", "docker"):
        out["docker"] = reap_docker(max_age_s=max_age_s, dry_run=dry_run)
    if backend in ("all", "microvm"):
        out["microvm"] = reap_microvm(max_age_s=max_age_s, dry_run=dry_run)
    if not out:
        raise ValueError(
            f"unknown backend {backend!r}: expected 'all', 'docker', or 'microvm'"
        )
    return out
