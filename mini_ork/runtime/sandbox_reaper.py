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
call — it only defines pure functions and fills two registries (dict inserts).

**Every teardown path dispatches through a registry** (cloud-providers A2), so a
new backend or cloud node provider is swept without editing a teardown module:

  * :func:`register_sandbox_reaper` — a TTL sweep, keyed by backend name. The
    built-ins ``docker`` / ``microvm`` register themselves below.
  * :func:`register_marker_reaper` — the immediate reap of a *specific* run's
    session, keyed by the ``backend`` field of ``<run_dir>/.workspace-session.json``.
    A backend whose module is heavy or optional declares itself in
    :data:`_LAZY_MARKER_REAPERS` instead, so the ``serve`` process running
    ``kill_run`` finds the hook without importing the backend up front.
  * Node providers (``mini_ork.remote.providers``) are swept too: their TTL
    sweep joins ``reap_sandboxes``.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_MAX_AGE_S",
    "known_sandbox_reapers",
    "register_marker_reaper",
    "register_sandbox_reaper",
    "reap_by_marker",
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
_SESSION_MARKER = ".workspace-session.json"  # written by workspace_session.py

# A TTL sweep: ``fn(*, max_age_s, dry_run) -> list[str]``.
SweepReaper = Callable[..., list[str]]
# A marker reap: ``fn(info: dict, run_dir: Path, *, dry_run) -> list[str]``.
MarkerReaper = Callable[..., list[str]]

# --- registries (module-global, filled at import by the built-ins) ------------

_SWEEP_REAPERS: dict[str, SweepReaper] = {}
_MARKER_REAPERS: dict[str, MarkerReaper] = {}
# Backends whose marker hook lives in a module we do not want to import at
# reaper-import time (a cloud SDK, a heavy transport). The value MUST be the
# exact importable module path; importing it registers the hook.
_LAZY_MARKER_REAPERS: dict[str, str] = {
    "remote": "mini_ork.runtime.backends.remote",
}


def register_sandbox_reaper(name: str, fn: SweepReaper) -> None:
    """Register ``fn`` as the TTL sweep for backend ``name`` (last write wins,
    like the other mini-ork registries). ``fn`` takes ``max_age_s`` / ``dry_run``
    as keyword arguments and returns the reaped ids; a raising ``fn`` is absorbed
    by :func:`reap_sandboxes` as ``[]``."""
    _SWEEP_REAPERS[name] = fn


def register_marker_reaper(backend: str, fn: MarkerReaper) -> None:
    """Register ``fn`` as the immediate marker reap for ``backend``.

    ``fn(marker_info, run_dir, *, dry_run)`` receives the parsed
    ``.workspace-session.json`` (so it can read its own keys) and the run's
    directory (where a backend's journals live), and returns the reaped ids.
    Called by :func:`reap_by_marker`; a raising ``fn`` is absorbed as ``[]``.
    """
    _MARKER_REAPERS[backend] = fn


def known_sandbox_reapers() -> list[str]:
    """Every registered sweep backend's name (``docker``/``microvm`` + plugins)."""
    return sorted(_SWEEP_REAPERS)


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


def _provider_sweep_names(backend: str) -> list[str]:
    """Which node providers ``backend`` asks :func:`reap_node_providers` to sweep.

    ``all`` sweeps every KNOWN provider, lazily importable ones included:
    ``sandbox-gc`` runs as a fresh process in which no provider has been
    imported yet, and a leaked cloud sandbox is exactly what the sweep exists
    for. That costs nothing on a host without the provider: a provider module
    imports no cloud SDK at import time, and its ``reap`` is fail-open (no SDK
    or no credential → ``[]``). Anything else is not a provider at all — the
    caller raises.
    """
    try:
        from mini_ork.remote.providers import base as providers
    except Exception:  # noqa: BLE001 — no provider package on this host: nothing to sweep
        return []
    if backend == "all":
        return providers.known_node_providers()
    if backend in providers.known_node_providers():
        return [backend]
    return []


def reap_sandboxes(
    *,
    backend: str = "all",
    max_age_s: int | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Dispatch the reap to one backend or all; return ``{backend: [ids]}``.

    ``backend`` is ``all`` (every registered sweep + every known node
    provider, lazily importable ones included), one registered sweep's name (``docker``,
    ``microvm``, or a plugin that called :func:`register_sandbox_reaper`), or a
    node provider's name (:func:`mini_ork.remote.providers.known_node_providers`).
    Each sweep is fail-open, so the result always has a key per requested
    backend even when nothing was reaped (or the backend is absent); an unknown
    name raises ``ValueError``.

    ``env`` is the provider scans' credential/selection environment (default
    ``os.environ``); the local sweeps read the process env themselves.
    """
    scope: Mapping[str, str] = env if env is not None else os.environ
    out: dict[str, list[str]] = {}
    for name, sweep in _SWEEP_REAPERS.items():
        if backend == "all" or backend == name:
            try:
                out[name] = list(sweep(max_age_s=max_age_s, dry_run=dry_run))
            except Exception:  # noqa: BLE001 — one backend's crash never fails the sweep
                out[name] = []
    provider_names = _provider_sweep_names(backend)
    if provider_names:
        from mini_ork.remote.providers import reap_node_providers

        out.update(
            reap_node_providers(
                max_age_s=_resolve_max_age(max_age_s),
                dry_run=dry_run,
                env=scope,
                names=provider_names,
            )
        )
    if not out:
        from mini_ork.remote.providers import known_node_providers

        raise ValueError(
            f"unknown backend {backend!r}: expected 'all', one of "
            f"{known_sandbox_reapers()}, or one of {known_node_providers()}"
        )
    return out


# --- marker reap (kill_run's immediate teardown) -------------------------------


def _reap_docker_marker(
    info: dict[str, Any],
    run_dir: Path,
    *,
    dry_run: bool = False,
) -> list[str]:
    """``docker rm -f <cid>`` for the session a marker records.

    This is the marker branch of :func:`reap_by_marker` as it stood before the
    registry, moved and not rewritten: same ``_run`` seam, same ``shutil.which``
    gate (a host with no docker CLI reports nothing rather than shelling out),
    same ``returncode == 0`` test, same fail-open on a vanished daemon.
    """
    del run_dir  # the container id is all the docker path needs
    cid = str(info.get("session_id") or "").strip()
    if not cid:
        return []
    if not shutil.which(_DOCKER_BIN):
        return []  # no docker on this host — nothing to reap, not an error
    if dry_run:
        return [cid]
    try:
        result = _run([_DOCKER_BIN, "rm", "-f", cid])
    except (FileNotFoundError, OSError):
        return []
    return [cid] if result.returncode == 0 else []


def _resolve_marker_reaper(backend: str) -> MarkerReaper | None:
    """The hook registered for ``backend``, importing a lazy backend on demand.

    ``None`` when the backend has no hook (an unknown backend, ``local``, …) or
    when its module cannot be imported — the caller then just unlinks the marker,
    exactly as this module did for every non-docker backend before the registry.
    """
    hook = _MARKER_REAPERS.get(backend)
    if hook is not None:
        return hook
    module = _LAZY_MARKER_REAPERS.get(backend)
    if not module:
        return None
    try:
        importlib.import_module(module)
    except Exception:  # noqa: BLE001 — an unloadable backend has no hook to run
        return None
    return _MARKER_REAPERS.get(backend)


def reap_by_marker(
    run_dir: str | os.PathLike[str],
    *,
    dry_run: bool = False,
) -> list[str]:
    """Reap whatever ``<run_dir>/.workspace-session.json`` records; idempotent.

    Remote-nodes-02 (D2): shortens the leak window for a killed run from the
    6 h TTL to immediate. Best-effort, never raises — a missing marker, an
    unreadable file, or an unknown backend returns ``[]``.

    The marker schema (``{backend, session_id, created_at, run_id}``) is written
    by :mod:`mini_ork.runtime.workspace_session`; this function is the read-side
    counterpart consumed by ``control.kill_run`` and the run-end sweep. The
    ``backend`` field selects the hook (:func:`register_marker_reaper`, or a
    lazily imported backend module), which receives the parsed marker and the
    run dir. The marker is unlinked after the hook runs — and left in place
    under ``dry_run`` — so a subsequent sweep does not re-read it. The TTL
    reaper stays as the safety net for crashes that never wrote the marker.
    """
    marker = Path(run_dir) / _SESSION_MARKER
    if not marker.is_file():
        return []
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(info, dict):
        return []
    hook = _resolve_marker_reaper(str(info.get("backend") or "").strip())
    reaped: list[str] = []
    if hook is not None:
        try:
            reaped = list(hook(info, Path(run_dir), dry_run=dry_run))
        except Exception:  # noqa: BLE001 — teardown never raises; TTL is the net
            reaped = []
    if not dry_run:
        try:
            marker.unlink()
        except OSError:
            pass
    return reaped


# The built-ins register themselves here — the one place a backend name lives.
# Dict inserts only: no I/O, no subprocess, no SDK import at module load.
register_sandbox_reaper("docker", reap_docker)
register_sandbox_reaper("microvm", reap_microvm)
register_marker_reaper("docker", _reap_docker_marker)
