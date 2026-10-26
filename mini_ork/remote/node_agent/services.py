"""Long-lived container services on a node — the app-runtime primitive.

Sessions (``sessions.py``) are *cattle*: one per agent run, torn down at the
end. A **service** is the other half of a deployment platform: a container
(or N replicas) that must stay up, publish a port, restart on failure, and
outlive the control plane that created it.

This module is deliberately mechanism-only. It knows how to turn a
:class:`ServiceSpec` into ``docker run`` argv, how to inspect/replace/delete
the containers, and how to tail their logs. It does **not** know about
tenants, billing, routing, domains, or which image a customer may run — those
are policy and live in the closed control plane (Attestia).

Contract (all routes are bearer-authed by the node-agent):

    POST   /v1/services              create or replace a service
    GET    /v1/services              list services on this node
    GET    /v1/services/{sid}        one service + replica state
    DELETE /v1/services/{sid}        stop and remove every replica
    POST   /v1/services/{sid}/restart
    GET    /v1/services/{sid}/logs?tail=N
    GET    /v1/services/{sid}/logs/stream?tail=N&replica=R   live NDJSON tail
    GET    /v1/services/{sid}/stats    one resource sample per running replica
    POST   /v1/services/{sid}/exec     run one argv inside replica 0
    POST   /v1/services/{sid}/volumes/{name}/snapshots
    GET    /v1/services/{sid}/volumes/{name}/snapshots
    DELETE /v1/services/{sid}/volumes/{name}/snapshots/{snap}
    GET    /v1/services/{sid}/volumes/{name}/snapshots/{snap}/blob
    PUT    /v1/services/{sid}/volumes/{name}/snapshots/{snap}/blob
    POST   /v1/services/{sid}/volumes/{name}/restore

Storage: ``<state_dir>/services.json``. Env **values** are never written to
disk — only the key names. A create request carries the values in memory and
hands them to ``docker run``; a later restart uses ``docker restart``, which
does not need them. This mirrors the D6 rule in ``procs.py``.

**Volumes** are the one durable thing a service owns, so they are deliberately
half-specified. A deployment document names a volume and the path it wants
inside the container; the node alone decides where on its disk that volume
lives — ``<volume_root>/<service_id>/<name>``. A control plane that sent a host
path could mount ``/etc`` or another tenant's directory into a container, and
no amount of path cleaning makes that a safe interface. ``volume_root`` is
``MO_NODE_VOLUME_ROOT`` when set (the secure tier: a backed-up, encrypted disk)
and ``<state_dir>/volumes`` otherwise (the simple tier: a laptop). A relative
env value raises rather than falling back — a volume that silently lands on the
container's ephemeral layer is data loss the tenant only discovers on redeploy.

**Shipping** is the control plane's job, not the node's. The node exposes the
bytes of a snapshot it already wrote (``GET …/blob``) and accepts bytes for a
snapshot id the control plane names (``PUT …/blob``), so an archive can leave
the machine that took it and come back on a different one. The node does not
decide where off-node bytes live, how many copies are kept, or when they
expire — those are retention policy. What the node does enforce is integrity:
an import must carry the sha256 of its body, is hashed while streaming to a
temporary file, and is renamed into place only when the bytes match. A repeat
import of the same content is a no-op that returns the existing metadata; a
different body for a taken id is refused, so a retried ship can never silently
replace a backup. Imports are bounded by ``MO_NODE_SNAPSHOT_MAX_BYTES``
(default 16 GiB), resolved at startup so a malformed value stops the node
rather than surfacing on the first restore of a real backup.

**Snapshots** are the durable copy of a volume. The node writes a ``tar.gz``
of the volume's contents under
``<snapshot_root>/<service_id>/<name>/<snapshot_id>.tar.gz`` plus a JSON
sidecar carrying the byte count and sha256; the control plane stores those
facts and later asks the node to restore by id. The snapshot root follows the
same two-tier rule as the volume root: ``MO_NODE_SNAPSHOT_ROOT`` when set (a
backed-up disk, a mounted object-store gateway), otherwise
``<state_dir>/volume-snapshots``. A snapshot of a running volume is
crash-consistent — the caller decides whether that is good enough — but a
restore is refused while any container still holds the volume, because
replacing the directory under a live process corrupts state instead of
recovering it.

**Networks** are how services reach each other without a public hostname. A
deployment document names a network and the DNS aliases the container answers
to inside it (``db``, ``db-1f2a3b4c.internal``); the node creates the network
if it is missing and joins every replica to it. The platform scopes the name
per environment, so two tenants (or two environments of one tenant) never share
a namespace. Driver choice is the node's: the default bridge is the
single-laptop tier, and ``MO_NODE_NETWORK_DRIVER=overlay`` on a Docker swarm
makes the same name span hosts — without the overlay, Docker networks stop at
the node boundary, so cross-node reachability is a fleet property, never
something this module can invent.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import re
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from .procs import _run as default_run

#: Docker name/label-safe id. The platform allocates these; the node does not
#: trust them beyond this shape (argv is a list, so this is defence in depth,
#: not the only injection barrier).
_SID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

#: Suffix shared by every container this module creates. The runtime reaper
#: uses the same label convention as sessions so a leaked service is sweepable.
_LABEL_SANDBOX = "mo.sandbox"
_LABEL_SERVICE = "mo.service"
_LABEL_DEPLOYMENT = "mo.deployment"

_RESTART_POLICIES = frozenset({"no", "on-failure", "always", "unless-stopped"})

#: Docker publishes ``/tcp`` by default, so ``tcp`` is never rendered.
_PORT_PROTOCOLS = ("tcp", "udp")

#: A volume name is an *identity*, not a path: it becomes one directory below
#: the node's volume root, so it is held to a stricter shape than the service id
#: (no dots, hence no ``..``) and never interpreted as anything but a leaf name.
_VOLUME_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

#: Container-side mount paths. Absolute, no ``:`` (Docker's field separator for
#: the ``-v host:container:opts`` triage), and no ``..`` segment — the same rule
#: the published schema states, enforced here as well because the node must not
#: depend on the caller having validated.
_MOUNT_PATH_RE = re.compile(r"^(/(?!\.\.?(/|$))[^:\x00/]+)+$")

#: Where a node keeps service volumes when the operator has not named a root.
_VOLUME_ROOT_ENV = "MO_NODE_VOLUME_ROOT"

#: Where a node keeps volume snapshots when the operator has not named a root.
_SNAPSHOT_ROOT_ENV = "MO_NODE_SNAPSHOT_ROOT"

#: Snapshot ids are minted by the node (``uuid4().hex``) and later received
#: back on restore/delete. Held to the minted shape so a request cannot name a
#: path component — the id is a leaf under a node-chosen directory.
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Cap on an imported snapshot body. An upload is written through to the
#: snapshot root, so without a bound a single request could fill the disk an
#: operator set aside for backups. 16 GiB clears a normal database volume and
#: is an operator lever (``MO_NODE_SNAPSHOT_MAX_BYTES``) for the rest.
_SNAPSHOT_MAX_BYTES_ENV = "MO_NODE_SNAPSHOT_MAX_BYTES"
_DEFAULT_SNAPSHOT_MAX_BYTES = 16 * 1024**3

#: The header an import must carry. Required, not optional: a body admitted
#: without its digest would be an archive whose bytes nothing verified, and it
#: would later restore as tenant data.
_SNAPSHOT_SHA256_HEADER = "x-content-sha256"
#: Canonical spelling for error messages; HTTP header lookup is case-insensitive.
_SNAPSHOT_SHA256_NAME = "X-Content-Sha256"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: A Docker network name. The platform sends an environment-scoped network so
#: services of one environment reach each other by name; the node may have to
#: create it. Held to a leaf shape (no dots, no slashes) even though it is
#: passed to ``docker network`` as argv, so a name that looks like a flag or a
#: path never reaches the daemon.
_NETWORK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

#: A network alias is a DNS name inside that network: ``db`` or
#: ``db-1f2a3b4c.internal``. Dots are part of the name and never a path hop;
#: empty labels (``a..b``, a trailing dot) are refused.
_NETWORK_ALIAS_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)

#: Driver used when the node has to create a network. ``bridge`` is the
#: single-laptop default; a fleet sets ``overlay`` (requires Docker swarm) so
#: one environment network spans hosts instead of being created per node.
_NETWORK_DRIVER_ENV = "MO_NODE_NETWORK_DRIVER"
_NETWORK_DRIVERS = frozenset({"bridge", "overlay"})

#: Bounds on a live log tail. ``tail`` is the history replayed before the
#: follow starts (Docker's own flag), and the queue is what sits between the
#: Docker child and a reader that may be slower than the container writes.
#: A full queue blocks the pump briefly and then checks whether the stream was
#: closed, so an abandoned tail stops reading instead of growing forever.
_LOG_TAIL_MAX = 5000
_LOG_QUEUE_FRAMES = 2000

#: Docker's ``--timestamps`` prefix: RFC3339 in UTC with a ``Z`` and optional
#: fractional seconds. Matching it means a line's timestamp is *Docker's clock*
#: rather than the node's read time, so two replicas can be merged in one
#: timeline without inventing an order.
_DOCKER_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (.*)$")


class ServiceError(ValueError):
    """A request the node refuses before it touches Docker."""


def _utc_now() -> str:
    """ISO-8601 UTC with a ``Z`` suffix (the wire style used elsewhere)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_log_line(replica: int, raw: str) -> dict[str, Any] | None:
    """One ``docker logs --timestamps`` line → a frame, or ``None`` to skip it.

    The prefix is Docker's, not the node's: a line that arrived from a restart
    before this tail started still carries the time the container wrote it, so
    a reader can order two replicas without trusting arrival order. A line
    without the prefix (a container that echoed something odd, or a Docker that
    formatted it differently) is passed through with ``ts: null`` rather than
    dropped — losing a log line is worse than not dating it.
    """
    line = raw.rstrip("\r\n")
    if not line.strip():
        # Docker emits an empty line for a blank container line; a frame that
        # says nothing is noise in a live stream.
        return None
    match = _DOCKER_TS_RE.match(line)
    if match is None:
        return {"replica": replica, "ts": None, "line": line}
    return {"replica": replica, "ts": match.group(1), "line": match.group(2)}


def _popen_logs(argv: list[str]) -> subprocess.Popen:
    """Start one ``docker logs --follow`` child, merged stdout/stderr.

    A child, not ``subprocess.run``: follow has no exit condition the node can
    put a deadline on, so the node owns the process and kills it when the
    reader goes away. Docker writes container stderr to the CLI's stderr, and a
    single merged stream is what a log viewer shows.
    """
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _port_mapping(port: Mapping[str, Any]) -> str:
    """Render one validated port entry as Docker's ``-p`` argument.

    Grammar: ``[bind:]host:container[/proto]`` — ``bind`` is an optional host
    IP, ``host`` may be empty for an ephemeral port, and ``/tcp`` is Docker's
    default so it is omitted. Keeping this pure is what lets the tests pin the
    full matrix (all-interfaces, bound, UDP, both) without a Docker daemon.
    """
    container = port["container"]
    host = port.get("host")
    bind = port.get("bind")
    target = f"{host}:{container}" if host is not None else f"{container}"
    if bind:
        # IPv6 literals need brackets, or the colons in the address read as
        # further ``-p`` separators.
        rendered = f"[{bind}]" if ":" in bind else bind
        # No host means an ephemeral host port: Docker spells it ``bind::c``.
        target = f"{rendered}:{host if host is not None else ''}:{container}"
    protocol = port.get("protocol") or "tcp"
    if protocol != "tcp":
        target = f"{target}/{protocol}"
    return target


#: Docker humanizes the numbers ``docker stats`` reports in two different base
#: systems: the I/O counters use decimal units (``kB`` = 1000 B) while memory
#: uses binary ones (``MiB`` = 1024 B). A parser that assumes one for both is
#: off by 2.4% at KiB and 7.4% at GiB — a graph that looks plausible and is
#: wrong. The units are part of the wire format, so they are mapped explicitly
#: rather than inferred from context.
_SIZE_UNITS = {
    "b": 1,
    "k": 1000, "m": 1000**2, "g": 1000**3, "t": 1000**4, "p": 1000**5,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "pb": 1000**5,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "pib": 1024**5,
}
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*$")


def _parse_size(raw: object) -> int | None:
    """``"12.3MiB"`` -> bytes. A shape Docker does not print yields ``None``.

    ``None`` rather than ``0``: "unread" and "genuinely zero" are different
    facts, and once both are zero nothing downstream can tell them apart.
    """
    if not isinstance(raw, str):
        return None
    match = _SIZE_RE.match(raw)
    if match is None:
        return None
    factor = _SIZE_UNITS.get((match.group(2) or "b").lower())
    if factor is None:
        return None
    return int(round(float(match.group(1)) * factor))


def _parse_pct(raw: object) -> float | None:
    """``"0.15%"`` -> ``0.15``. Anything else is unread, not zero."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.endswith("%"):
        return None
    try:
        return round(float(text[:-1]), 4)
    except ValueError:
        return None


def _split_size_pair(raw: object) -> tuple[int | None, int | None]:
    """``"1.2kB / 3.4MB"`` -> ``(1200, 3400000)``; a missing half is ``None``."""
    if not isinstance(raw, str) or "/" not in raw:
        return (None, None)
    left, _, right = raw.partition("/")
    return (_parse_size(left), _parse_size(right))


def _parse_stats_line(line: str) -> dict[str, Any] | None:
    """One ``docker stats --format '{{json .}}'`` line -> a flat sample.

    The wire fields keep Docker's names and pairs; this is the one place the
    two-half strings are split, so the caller never parses presentation text.
    """
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, dict):
        return None
    name = row.get("Name")
    if not isinstance(name, str) or not name:
        return None
    mem_used, mem_limit = _split_size_pair(row.get("MemUsage"))
    net_rx, net_tx = _split_size_pair(row.get("NetIO"))
    block_read, block_write = _split_size_pair(row.get("BlockIO"))
    pids = row.get("PIDs")
    return {
        "name": name,
        "cpu_pct": _parse_pct(row.get("CPUPerc")),
        "mem_pct": _parse_pct(row.get("MemPerc")),
        "mem_used_bytes": mem_used,
        "mem_limit_bytes": mem_limit,
        "net_rx_bytes": net_rx,
        "net_tx_bytes": net_tx,
        "block_read_bytes": block_read,
        "block_write_bytes": block_write,
        "pids": int(pids) if isinstance(pids, str) and pids.isdigit() else None,
    }


def _run_input(
    argv: list[str], *, stdin: str = "", timeout: float = 30
) -> subprocess.CompletedProcess:
    """Run argv with stdin attached — the seam for control-plane exec.

    Separate from :func:`procs._run` because that helper has no way to hand a
    child bytes, and a credential must travel on stdin rather than in argv
    where ``ps`` on the node would show it. Mirrors the same
    ``capture_output``/``text``/``check=False`` contract so callers treat the
    result identically.
    """
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _extract_safely(archive: Path, dest: Path) -> None:
    """Extract a snapshot without letting a member escape ``dest``.

    ``filter='data'`` (3.11.4+/3.12) is the interpreter's own allow-list: it
    rejects absolute paths, ``..`` hops, device nodes, and setuid bits. Runtimes
    without the filter fall back to an explicit allow-list of regular files and
    directories, so an old node is stricter, never looser.
    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(dest, filter="data")
        return
    except TypeError:
        pass  # interpreter predates the `filter` argument
    except tarfile.TarError as exc:
        raise ServiceError(f"snapshot archive is unreadable: {exc}") from exc
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise ServiceError(f"snapshot contains an unsafe path: {member.name!r}")
            if not (member.isfile() or member.isdir()):
                raise ServiceError(f"snapshot contains a non-regular entry: {member.name!r}")
        tar.extractall(dest)


class VolumeNotMaterializedError(ServiceError):
    """The volume has no directory on this node, so there is nothing to copy."""


class VolumeInUseError(ServiceError):
    """A container still holds the volume; restoring under it would corrupt it."""


class SnapshotNotFoundError(ServiceError):
    """No snapshot with that id exists for this service and volume."""


class SnapshotExistsError(ServiceError):
    """That snapshot id is taken by content the import did not match.

    A repeat import of the *same* bytes is not an error — it returns the
    existing metadata — but a different body must never overwrite a backup.
    """


class SnapshotTooLargeError(ServiceError):
    """An imported body exceeds ``MO_NODE_SNAPSHOT_MAX_BYTES``."""


class ServiceRestartFailedError(RuntimeError):
    """A quiesced snapshot exists, but a replica did not come back up.

    The archive is durable and listed — the copy is real — but leaving the
    service down silently would be worse than an error. The message names the
    snapshot so the operator can act on both facts.
    """


class ServiceExecTimeoutError(RuntimeError):
    """A control-plane exec outlived its bound and was killed.

    Distinct from a non-zero exit: the command did not answer, so the caller
    cannot conclude that it failed at its own work.
    """


#: Grace period Docker gives a container to exit on ``docker stop``. A database
#: that needs longer than this to flush is doing something the node cannot
#: speed up; ten seconds matches Docker's own default and is long enough for
#: Postgres' fast shutdown.
_QUIESCE_STOP_SECONDS = "10"


@dataclass
class ServiceSpec:
    """The durable half of a service. Env values are intentionally absent."""

    service_id: str
    image: str
    command: list[str] = field(default_factory=list)
    env_keys: list[str] = field(default_factory=list)
    ports: list[dict[str, Any]] = field(default_factory=list)
    #: ``[{"name": ..., "mount_path": ...}]`` — the host side is derived, never
    #: stored, so a state file copied to another node re-derives its own paths.
    volumes: list[dict[str, str]] = field(default_factory=list)
    replicas: int = 1
    cpus: float | None = None
    memory: str | None = None
    restart_policy: str = "unless-stopped"
    labels: dict[str, str] = field(default_factory=dict)
    healthcheck: dict[str, Any] | None = None
    network: str | None = None
    #: DNS names the container answers to inside ``network``. The platform's
    #: stable name for a service lives here; they are meaningless without a
    #: network, so validation refuses the combination.
    network_aliases: list[str] = field(default_factory=list)
    user: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ServiceSpec":
        return cls(
            service_id=str(data["service_id"]),
            image=str(data["image"]),
            command=[str(a) for a in data.get("command") or []],
            env_keys=[str(k) for k in data.get("env_keys") or []],
            ports=[dict(p) for p in data.get("ports") or []],
            volumes=[
                {"name": str(v["name"]), "mount_path": str(v["mount_path"])}
                for v in data.get("volumes") or []
            ],
            replicas=int(data.get("replicas") or 1),
            cpus=float(data["cpus"]) if data.get("cpus") is not None else None,
            memory=str(data["memory"]) if data.get("memory") else None,
            restart_policy=str(data.get("restart_policy") or "unless-stopped"),
            labels={str(k): str(v) for k, v in (data.get("labels") or {}).items()},
            healthcheck=dict(data["healthcheck"]) if data.get("healthcheck") else None,
            network=str(data["network"]) if data.get("network") else None,
            network_aliases=[str(a) for a in data.get("network_aliases") or []],
            user=str(data["user"]) if data.get("user") else None,
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )


def _validate(payload: Mapping[str, Any]) -> tuple[ServiceSpec, dict[str, str]]:
    version = payload.get("schema_version")
    if version is not None and version != "mini-ork.deployment/v1":
        raise ServiceError(
            f"unsupported schema_version {version!r}; expected 'mini-ork.deployment/v1'"
        )
    sid = str(payload.get("service_id") or "")
    if not _SID_RE.match(sid):
        raise ServiceError(
            "service_id must match ^[a-z0-9][a-z0-9-]{0,62}$"
        )
    image = str(payload.get("image") or "").strip()
    if not image:
        raise ServiceError("image is required")
    if any(ch in image for ch in (" ", "\n", "\t")):
        raise ServiceError("image must not contain whitespace")

    replicas = int(payload.get("replicas") or 1)
    if replicas < 1 or replicas > 100:
        raise ServiceError("replicas must be between 1 and 100")

    policy = str(payload.get("restart_policy") or "unless-stopped")
    if policy not in _RESTART_POLICIES:
        raise ServiceError(
            f"restart_policy must be one of {sorted(_RESTART_POLICIES)}"
        )

    ports: list[dict[str, Any]] = []
    for raw in payload.get("ports") or []:
        if not isinstance(raw, Mapping):
            raise ServiceError("ports must be a list of objects")
        container = int(raw.get("container") or 0)
        if container < 1 or container > 65535:
            raise ServiceError("port.container must be 1..65535")
        host_raw = raw.get("host")
        host = int(host_raw) if host_raw is not None else None
        if host is not None and (host < 1 or host > 65535):
            raise ServiceError("port.host must be 1..65535")
        protocol = str(raw.get("protocol") or "tcp").lower()
        if protocol not in _PORT_PROTOCOLS:
            raise ServiceError(
                f"port.protocol must be one of {list(_PORT_PROTOCOLS)}: {protocol!r}"
            )
        bind = raw.get("bind")
        if bind is not None:
            # A literal address only: a hostname here would make the container
            # bind to whatever DNS says at spawn time, which is neither what the
            # caller asked for nor reproducible across nodes.
            try:
                bind = str(ipaddress.ip_address(str(bind)))
            except ValueError:
                raise ServiceError(
                    f"port.bind must be an IP literal: {bind!r}"
                ) from None
        # Only non-default fields are stored, so an existing service.json
        # written before this contract decodes to exactly the same spec.
        entry: dict[str, Any] = {"container": container, "host": host}
        if protocol != "tcp":
            entry["protocol"] = protocol
        if bind is not None:
            entry["bind"] = bind
        ports.append(entry)

    # Two collisions Docker would accept without a word, and both lose data: the
    # same name twice mounts one directory at two paths, and the same mount path
    # twice mounts the second volume *over* the first. Refused here so the
    # caller sees a 400 instead of a service that silently lost its state.
    volumes: list[dict[str, str]] = []
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    for raw in payload.get("volumes") or []:
        if not isinstance(raw, Mapping):
            raise ServiceError("volumes must be a list of objects")
        name = str(raw.get("name") or "")
        if not _VOLUME_NAME_RE.match(name):
            raise ServiceError(
                f"volume name must match ^[a-z0-9][a-z0-9_-]{{0,62}}$: {name!r}"
            )
        mount_path = str(raw.get("mount_path") or "")
        if not _MOUNT_PATH_RE.match(mount_path):
            raise ServiceError(
                "volume mount_path must be an absolute path without ':' or '..': "
                f"{mount_path!r}"
            )
        if name in seen_names:
            raise ServiceError(f"duplicate volume name: {name!r}")
        if mount_path in seen_paths:
            raise ServiceError(f"two volumes mount the same path: {mount_path!r}")
        seen_names.add(name)
        seen_paths.add(mount_path)
        volumes.append({"name": name, "mount_path": mount_path})

    # Private networking: a network name is an identity the node may have to
    # create, and an alias only means something inside that network. Refusing
    # the pair-less case here keeps a caller from believing it published a DNS
    # name when Docker would have ignored it on the default bridge.
    network = str(payload["network"]).strip() if payload.get("network") else None
    if network is not None and not _NETWORK_NAME_RE.match(network):
        raise ServiceError(
            f"network must match ^[a-z0-9][a-z0-9_-]{{0,62}}$: {network!r}"
        )
    network_aliases: list[str] = []
    for raw in payload.get("network_aliases") or []:
        alias = str(raw).strip()
        if len(alias) > 253 or not _NETWORK_ALIAS_RE.match(alias):
            raise ServiceError(f"network alias is not a DNS name: {alias!r}")
        if alias not in network_aliases:
            network_aliases.append(alias)
    if len(network_aliases) > 32:
        raise ServiceError("at most 32 network aliases are supported")
    if network_aliases and network is None:
        raise ServiceError("network_aliases requires network")

    resources = payload.get("resources") or {}
    cpus_raw = resources.get("cpus", payload.get("cpus"))
    memory_raw = resources.get("memory", payload.get("memory"))

    env = payload.get("env") or {}
    if not isinstance(env, Mapping):
        raise ServiceError("env must be an object of string values")
    env = {str(k): str(v) for k, v in env.items()}

    env_keys = sorted(set(map(str, payload.get("env_keys") or [])) | set(env))
    for key in env_keys:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ServiceError(f"invalid env key: {key!r}")

    labels = payload.get("labels") or {}
    if not isinstance(labels, Mapping):
        raise ServiceError("labels must be an object")
    labels = {str(k): str(v) for k, v in labels.items()}
    # The deployment id is a first-class field in the contract; it becomes the
    # label the control plane uses to correlate containers with a deploy.
    deployment = payload.get("deployment_id")
    if deployment:
        labels["mo.deployment"] = str(deployment)

    healthcheck = payload.get("healthcheck")
    if healthcheck is not None and not isinstance(healthcheck, Mapping):
        raise ServiceError("healthcheck must be an object")

    now = time.time()
    spec = ServiceSpec(
        service_id=sid,
        image=image,
        command=[str(a) for a in payload.get("command") or []],
        env_keys=env_keys,
        ports=ports,
        volumes=volumes,
        replicas=replicas,
        cpus=float(cpus_raw) if cpus_raw is not None else None,
        memory=str(memory_raw) if memory_raw else None,
        restart_policy=policy,
        labels={str(k): str(v) for k, v in labels.items()},
        healthcheck=dict(healthcheck) if healthcheck else None,
        network=network,
        network_aliases=network_aliases,
        user=str(payload["user"]) if payload.get("user") else None,
        created_at=now,
        updated_at=now,
    )
    return spec, env


class ServiceManager:
    """Docker-backed long-lived services, one manager per node-agent."""

    def __init__(
        self,
        state_dir: Path,
        *,
        volume_root: Path | None = None,
        snapshot_root: Path | None = None,
        snapshot_max_bytes: int | None = None,
        _run_fn: Callable[..., Any] | None = None,
        _run_input_fn: Callable[..., Any] | None = None,
        _popen_fn: Callable[[list[str]], subprocess.Popen] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._run_input = _run_input_fn or _run_input
        # Separate from ``_run_fn``: a follow stream is a live child the node
        # kills, not a command it waits on, so tests seam it separately.
        self._popen = _popen_fn or _popen_logs
        self.volume_root = self._resolve_volume_root(volume_root)
        self.snapshot_root = self._resolve_snapshot_root(snapshot_root)
        self.snapshot_max_bytes = self._resolve_snapshot_max_bytes(snapshot_max_bytes)
        self._path = self.state_dir / "services.json"
        self._run = _run_fn or default_run
        self._specs: dict[str, ServiceSpec] = {}
        #: service id -> {replica index -> container id}
        self._cids: dict[str, dict[int, str]] = {}
        self._load()

    # ---- persistence -------------------------------------------------

    def _resolve_volume_root(self, explicit: Path | None) -> Path:
        """The directory service volumes live under. Fails rather than guesses.

        Precedence: an explicit argument, then ``MO_NODE_VOLUME_ROOT``, then a
        ``volumes/`` directory beside the service state. The environment is the
        secure tier — an operator pointing tenant data at a backed-up disk — and
        a *relative* value there is a misconfiguration, not a request to use the
        cwd: falling back would put the volumes somewhere the operator did not
        choose, and the tenant would not find out until a redeploy emptied them.
        """
        if explicit is not None:
            return Path(explicit)
        raw = os.environ.get(_VOLUME_ROOT_ENV)
        if raw:
            root = Path(raw)
            if not root.is_absolute():
                raise ServiceError(
                    f"{_VOLUME_ROOT_ENV} must be an absolute path, got {raw!r}"
                )
            return root
        return self.state_dir / "volumes"

    def _resolve_snapshot_root(self, explicit: Path | None) -> Path:
        """Where snapshots live; same precedence and fail-closed rule as volumes.

        The snapshot root is the second half of the durability story: on the
        simple tier it sits beside the volume state on one laptop disk, and on
        the secure tier ``MO_NODE_SNAPSHOT_ROOT`` points at a disk that is
        backed up independently of the volume root. If the two roots are the
        same disk, a backup is not a backup — the operator decides, and the
        node records where each snapshot went rather than guessing.
        """
        if explicit is not None:
            return Path(explicit)
        raw = os.environ.get(_SNAPSHOT_ROOT_ENV)
        if raw:
            root = Path(raw)
            if not root.is_absolute():
                raise ServiceError(
                    f"{_SNAPSHOT_ROOT_ENV} must be an absolute path, got {raw!r}"
                )
            return root
        return self.state_dir / "volume-snapshots"

    def _resolve_snapshot_max_bytes(self, explicit: int | None) -> int:
        """Bound on an imported snapshot. A malformed value refuses startup.

        Imported bytes are written to the snapshot root before anything checks
        them, so the bound is a disk-safety limit, not an aesthetic one. The
        same fail-closed rule as the roots: an unreadable setting is an operator
        error, and silently substituting the default would leave a node the
        operator believed was bounded.
        """
        if explicit is not None:
            value = explicit
        else:
            raw = os.environ.get(_SNAPSHOT_MAX_BYTES_ENV)
            if raw is None or raw.strip() == "":
                return _DEFAULT_SNAPSHOT_MAX_BYTES
            try:
                value = int(raw)
            except ValueError as exc:
                raise ServiceError(
                    f"{_SNAPSHOT_MAX_BYTES_ENV} must be a positive integer, got {raw!r}"
                ) from exc
        if value <= 0:
            raise ServiceError(
                f"{_SNAPSHOT_MAX_BYTES_ENV} must be a positive integer, got {value!r}"
            )
        return value

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("services", []):
            try:
                spec = ServiceSpec.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            self._specs[spec.service_id] = spec
            self._cids[spec.service_id] = {
                int(k): str(v)
                for k, v in (item.get("cids") or {}).items()
            }

    def _save(self) -> None:
        payload = {
            "services": [
                {**spec.to_dict(), "cids": self._cids.get(spec.service_id, {})}
                for spec in self._specs.values()
            ]
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    # ---- naming / argv ------------------------------------------------

    def _name(self, spec: ServiceSpec, replica: int) -> str:
        return f"mo-svc-{spec.service_id}-{replica}"

    def host_volume_path(self, spec: ServiceSpec, volume_name: str) -> str:
        """Where a volume lives on this node's disk — derived, never received.

        ``service_id`` and ``volume_name`` are both regex-restricted leaf names
        (no ``.``, no ``/``), so this join cannot escape ``volume_root``; that
        is the reason the wire carries a name rather than a path.
        """
        return str(self.volume_root / spec.service_id / volume_name)

    # ---- volume snapshots ---------------------------------------------

    def _volume_dir(self, service_id: str, volume_name: str) -> Path:
        """Derive a volume's directory from names alone.

        Deliberately does not require a live ``ServiceSpec``: a stopped service
        still owns its data, and the data outlives a ``DELETE /v1/services`` by
        design (retention is a platform decision). Both components are leaf
        names under the node's roots, so the join cannot escape either root.
        """
        if not _SID_RE.match(service_id):
            raise ServiceError(
                f"service_id must match ^[a-z0-9][a-z0-9-]{{0,62}}$: {service_id!r}"
            )
        if not _VOLUME_NAME_RE.match(volume_name):
            raise ServiceError(
                f"volume name must match ^[a-z0-9][a-z0-9_-]{{0,62}}$: {volume_name!r}"
            )
        return self.volume_root / service_id / volume_name

    def _snapshot_dir(self, service_id: str, volume_name: str) -> Path:
        return self.snapshot_root / service_id / volume_name

    @staticmethod
    def _snapshot_id(raw: object) -> str:
        value = str(raw or "")
        if not _SNAPSHOT_ID_RE.match(value):
            raise ServiceError(f"snapshot id must be 32 lowercase hex chars: {value!r}")
        return value

    def _stop_for_copy(self, service_id: str) -> list[str]:
        """Stop every replica that could write to the volume; return their names.

        Fail closed. The caller asked for a copy no writer can be half-way
        through; if a container will not stop, resuming the ones already
        stopped and refusing is the only answer that does not quietly produce a
        crash-consistent archive labelled consistent. A replica Docker reports
        as *missing* or *exited* holds no open files, so nothing is stopped for
        it — and a service this node does not define cannot be quiesced at all,
        because there is nothing here to stop.
        """
        if service_id not in self._specs:
            raise ServiceError(
                f"service {service_id!r} is not defined on this node; cannot quiesce it"
            )
        stopped: list[str] = []
        for name, state in self._volume_holders(service_id):
            result = self._run(["docker", "stop", "--time", _QUIESCE_STOP_SECONDS, name])
            if getattr(result, "returncode", 0) != 0:
                self._resume(stopped)
                detail = (getattr(result, "stderr", "") or "").strip()
                raise ServiceError(
                    f"could not stop {name} ({state}) to quiesce the volume: {detail}"
                )
            stopped.append(name)
        return stopped

    def _resume(self, names: list[str]) -> list[str]:
        """Start the replicas a quiesced copy stopped; return the failures."""
        failed: list[str] = []
        for name in names:
            result = self._run(["docker", "start", name])
            if getattr(result, "returncode", 0) != 0:
                failed.append(name)
        return failed

    def snapshot(
        self, service_id: str, volume_name: str, *, quiesce: bool = False
    ) -> dict[str, Any]:
        """Write a ``tar.gz`` of the volume and its metadata sidecar.

        Refuses when the volume has never been materialized on this node (no
        directory): an empty archive would look like a successful backup and
        restore to nothing. An existing but empty directory *is* a real
        snapshot — "the volume was empty when I copied it" is a fact worth
        keeping.

        ``quiesce`` stops the replicas that hold the mount before the copy and
        starts them again afterwards, so the archive is engine-consistent
        rather than crash-consistent: no writer was mid-transaction when the
        bytes were read. It is opt-in because it costs downtime for the length
        of the copy, and the policy — when consistency is worth that — belongs
        to the control plane. The sidecar records whether the copy was quiesced
        so a backup list never implies more than happened.
        """
        source = self._volume_dir(service_id, volume_name)
        if not source.is_dir():
            raise VolumeNotMaterializedError(
                f"volume {volume_name!r} of service {service_id!r} has no data on this node"
            )

        out_dir = self._snapshot_dir(service_id, volume_name)
        try:
            out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise RuntimeError(f"could not create snapshot directory {out_dir}: {exc}") from exc

        snapshot_id = uuid.uuid4().hex
        archive = out_dir / f"{snapshot_id}.tar.gz"
        tmp = out_dir / f".{snapshot_id}.tar.gz.tmp"
        stopped: list[str] = []
        restart_failed: list[str] = []
        try:
            if quiesce:
                stopped = self._stop_for_copy(service_id)
            with tarfile.open(tmp, "w:gz") as tar:
                for child in sorted(source.iterdir()):
                    tar.add(child, arcname=child.name, recursive=True)
            digest = hashlib.sha256()
            size = 0
            with tmp.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
                    size += len(chunk)
            os.replace(tmp, archive)
        finally:
            # Restarting is unconditional: an exception during the copy must
            # not leave the service down. The failure list is carried out of
            # the ``finally`` so a successful copy can still report it.
            restart_failed = self._resume(stopped)
            tmp.unlink(missing_ok=True)

        meta = {
            "snapshot_id": snapshot_id,
            "service_id": service_id,
            "volume": volume_name,
            "bytes": size,
            "sha256": digest.hexdigest(),
            "created_at": _utc_now(),
            "quiesced": bool(quiesce),
        }
        if stopped:
            meta["quiesce_stopped"] = len(stopped)
        if restart_failed:
            meta["quiesce_restart_failed"] = restart_failed
        # The sidecar is written after the archive is durable. A crash between
        # the two leaves an archive that listing ignores and restore refuses —
        # an invisible snapshot, never a listed one that cannot be restored.
        meta_tmp = out_dir / f".{snapshot_id}.json.tmp"
        meta_tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(meta_tmp, out_dir / f"{snapshot_id}.json")
        if restart_failed:
            # The bytes are a real, listed backup; the service is not running.
            # Both facts are true and the caller must see both.
            raise ServiceRestartFailedError(
                f"snapshot {snapshot_id} is complete, but "
                f"{', '.join(restart_failed)} did not restart; "
                "start the replica before the next deploy"
            )
        return meta

    def list_snapshots(self, service_id: str, volume_name: str) -> list[dict[str, Any]]:
        """Snapshots for one volume, newest first. Read exclusively from sidecars."""
        self._volume_dir(service_id, volume_name)
        out_dir = self._snapshot_dir(service_id, volume_name)
        if not out_dir.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for meta_path in out_dir.glob("*.json"):
            archive = meta_path.with_suffix(".tar.gz")
            if not archive.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(meta, dict) and meta.get("snapshot_id"):
                found.append(meta)
        found.sort(key=lambda m: (str(m.get("created_at") or ""), str(m["snapshot_id"])), reverse=True)
        return found

    def restore(self, service_id: str, volume_name: str, snapshot_id: object) -> dict[str, Any]:
        """Replace the volume's contents with a snapshot, atomically.

        Refused while a container still holds the mount. The check fails
        closed: a state this module cannot read is treated as holding the
        volume, because the cost of a wrong restore is the tenant's data.
        """
        self._volume_dir(service_id, volume_name)
        snap = self._snapshot_id(snapshot_id)
        out_dir = self._snapshot_dir(service_id, volume_name)
        archive = out_dir / f"{snap}.tar.gz"
        meta_path = out_dir / f"{snap}.json"
        if not archive.is_file() or not meta_path.is_file():
            raise SnapshotNotFoundError(
                f"no snapshot {snap!r} for volume {volume_name!r} of service {service_id!r}"
            )
        holders = self._volume_holders(service_id)
        if holders:
            raise VolumeInUseError(
                "refusing to restore while containers hold the volume: "
                + ", ".join(f"{name} ({state})" for name, state in holders)
            )
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceError(f"snapshot {snap!r} has unreadable metadata: {exc}") from exc

        target = self.volume_root / service_id / volume_name
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = target.parent / f".{volume_name}.restore-{uuid.uuid4().hex}"
        swapped = target.parent / f".{volume_name}.pre-restore-{uuid.uuid4().hex}"
        stage.mkdir(parents=True, mode=0o700)
        try:
            _extract_safely(archive, stage)
            if target.exists():
                os.replace(target, swapped)
            os.replace(stage, target)
        except Exception:
            # Put the old contents back if the swap did not complete, so a
            # failed restore leaves the volume exactly as it was.
            if swapped.exists() and not target.exists():
                os.replace(swapped, target)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(swapped, ignore_errors=True)

        restored = {
            "restored": True,
            "snapshot_id": snap,
            "service_id": service_id,
            "volume": volume_name,
            "bytes": meta.get("bytes"),
            "sha256": meta.get("sha256"),
        }
        return restored

    def delete_snapshot(self, service_id: str, volume_name: str, snapshot_id: object) -> bool:
        """Remove one snapshot. Reports whether it existed rather than guessing."""
        self._volume_dir(service_id, volume_name)
        snap = self._snapshot_id(snapshot_id)
        out_dir = self._snapshot_dir(service_id, volume_name)
        archive = out_dir / f"{snap}.tar.gz"
        meta_path = out_dir / f"{snap}.json"
        existed = archive.is_file() or meta_path.is_file()
        archive.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        try:
            out_dir.rmdir()
        except OSError:
            pass
        if not existed:
            raise SnapshotNotFoundError(
                f"no snapshot {snap!r} for volume {volume_name!r} of service {service_id!r}"
            )
        return True

    def snapshot_archive(
        self, service_id: str, volume_name: str, snapshot_id: object
    ) -> tuple[Path, dict[str, Any]]:
        """The archive and metadata of one snapshot, for shipping off-node.

        Both files must exist. An archive without a sidecar is what a crash
        between the two writes leaves behind: listing ignores it and restore
        refuses it, and handing those bytes to a control plane would let it
        record a backup it can never restore. Reading the sidecar here means
        the caller ships a snapshot the node itself considers restorable.
        """
        self._volume_dir(service_id, volume_name)
        snap = self._snapshot_id(snapshot_id)
        out_dir = self._snapshot_dir(service_id, volume_name)
        archive = out_dir / f"{snap}.tar.gz"
        meta_path = out_dir / f"{snap}.json"
        if not archive.is_file() or not meta_path.is_file():
            raise SnapshotNotFoundError(
                f"no snapshot {snap!r} for volume {volume_name!r} of service {service_id!r}"
            )
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceError(f"snapshot {snap!r} has unreadable metadata: {exc}") from exc
        if not isinstance(meta, dict):
            raise ServiceError(f"snapshot {snap!r} has unreadable metadata")
        return archive, meta

    async def import_snapshot(
        self,
        service_id: str,
        volume_name: str,
        snapshot_id: object,
        chunks: AsyncIterator[bytes],
        *,
        sha256: str,
    ) -> dict[str, Any]:
        """Accept an archive the control plane shipped, under an id it names.

        The write order mirrors :meth:`snapshot`: bytes stream to a temporary
        file, are hashed on the way, are fsynced, renamed into place, and only
        then does the sidecar exist. A failure at any point leaves no listed
        snapshot — never one whose bytes differ from its metadata.

        Idempotent by content: a retry that delivers the same sha256 returns
        the existing record instead of failing, which is what makes a ship
        retried after a dropped response safe. Different bytes for the same id
        are refused: overwriting a backup under a name the control plane already
        recorded would be the quiet kind of data loss.
        """
        self._volume_dir(service_id, volume_name)
        snap = self._snapshot_id(snapshot_id)
        digest_hex = str(sha256 or "")
        if not _SHA256_RE.match(digest_hex):
            raise ServiceError(
                f"{_SNAPSHOT_SHA256_NAME} must be 64 lowercase hex characters"
            )

        out_dir = self._snapshot_dir(service_id, volume_name)
        archive = out_dir / f"{snap}.tar.gz"
        meta_path = out_dir / f"{snap}.json"
        if archive.is_file() or meta_path.is_file():
            if not (archive.is_file() and meta_path.is_file()):
                raise SnapshotExistsError(
                    f"snapshot {snap!r} is incomplete on this node; delete it before importing"
                )
            try:
                existing = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SnapshotExistsError(
                    f"snapshot {snap!r} has unreadable metadata; delete it before importing"
                ) from exc
            if isinstance(existing, dict) and existing.get("sha256") == digest_hex:
                return {**existing, "imported": False}
            raise SnapshotExistsError(
                f"snapshot {snap!r} already exists with different content on this node"
            )

        try:
            out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise RuntimeError(f"could not create snapshot directory {out_dir}: {exc}") from exc

        tmp = out_dir / f".{snap}.tar.gz.import-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        size = 0
        try:
            with tmp.open("wb") as fh:
                async for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise ServiceError("snapshot body must be binary")
                    size += len(chunk)
                    if size > self.snapshot_max_bytes:
                        raise SnapshotTooLargeError(
                            f"snapshot body exceeds {self.snapshot_max_bytes} bytes "
                            f"({_SNAPSHOT_MAX_BYTES_ENV})"
                        )
                    digest.update(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            if digest.hexdigest() != digest_hex:
                raise ServiceError(
                    f"snapshot body sha256 {digest.hexdigest()} does not match "
                    f"{_SNAPSHOT_SHA256_NAME} {digest_hex}"
                )
            os.replace(tmp, archive)
        finally:
            tmp.unlink(missing_ok=True)

        meta = {
            "snapshot_id": snap,
            "service_id": service_id,
            "volume": volume_name,
            "bytes": size,
            "sha256": digest_hex,
            "created_at": _utc_now(),
        }
        meta_tmp = out_dir / f".{snap}.json.import-{uuid.uuid4().hex}"
        try:
            meta_tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(meta_tmp, meta_path)
        finally:
            meta_tmp.unlink(missing_ok=True)
        return {**meta, "imported": True}

    def _volume_holders(self, service_id: str) -> list[tuple[str, str]]:
        """Containers whose mounts still point at the volume.

        Only *missing*, *exited* and *dead* are safe: every other Docker state
        (created, restarting, running, paused, removing) either has the mount
        live now or will shortly. An unreadable state counts as holding.
        """
        spec = self._specs.get(service_id)
        if spec is None:
            return []
        holders: list[tuple[str, str]] = []
        for replica in range(max(spec.replicas, 1)):
            name = self._name(spec, replica)
            state = self._replica_state(name)
            if state not in ("missing", "exited", "dead"):
                holders.append((name, state))
        return holders

    def _ensure_network(self, name: str) -> None:
        """Create the service network if this node does not have it yet.

        Idempotent by inspection, not by swallowing an error: a second deploy
        joins the existing network and never re-creates it. The driver is the
        node's choice — the default bridge is enough on one host, and a fleet
        sets ``MO_NODE_NETWORK_DRIVER=overlay`` so the same environment network
        spans nodes (overlay needs Docker swarm; a node that is not in a swarm
        fails here, loudly, instead of containers landing on separate bridges
        and silently not reaching each other).
        """
        result = self._run(["docker", "network", "inspect", name])
        if int(getattr(result, "returncode", 1)) == 0:
            return
        driver = (os.environ.get(_NETWORK_DRIVER_ENV) or "bridge").strip() or "bridge"
        # A bad driver is an operator mistake and a daemon refusal is an
        # infrastructure one; neither is the caller's fault, so both surface as
        # 500 rather than a 400 that would send the tenant looking at its spec.
        if driver not in _NETWORK_DRIVERS:
            raise RuntimeError(
                f"{_NETWORK_DRIVER_ENV} must be one of {sorted(_NETWORK_DRIVERS)}: "
                f"{driver!r}"
            )
        result = self._run(["docker", "network", "create", "--driver", driver, name])
        if int(getattr(result, "returncode", 1)) != 0:
            stderr = (getattr(result, "stderr", "") or "").strip()
            stdout = (getattr(result, "stdout", "") or "").strip()
            raise RuntimeError(
                f"docker network create failed ({result.returncode}): {stderr or stdout}"
            )

    def _prepare_volumes(self, spec: ServiceSpec) -> None:
        """Create each volume's host directory before Docker sees it.

        Docker creates a missing bind source as root and, on some setups, as a
        *file*; a service that then fails to write looks like an application bug
        rather than a node one. Creating them here (0700) makes the failure
        either absent or immediate and attributable. Existing directories are
        left as they are — an operator may have mounted a disk or set a policy
        there, and this is not the place to overrule it.
        """
        for volume in spec.volumes:
            path = Path(self.host_volume_path(spec, volume["name"]))
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise RuntimeError(
                    f"could not create volume directory {path}: {exc}"
                ) from exc

    def _env_for(self, spec: ServiceSpec, env: Mapping[str, str]) -> dict[str, str]:
        """Values from the request win; otherwise resolve from the node env.

        Only allowlisted keys cross into the container (D6). ``PATH`` is not
        injected — the image's own ENV is the container's PATH.
        """
        resolved: dict[str, str] = {}
        for key in spec.env_keys:
            if key in env:
                resolved[key] = str(env[key])
            elif key in os.environ:
                resolved[key] = os.environ[key]
        return resolved

    def _docker_run_argv(
        self, spec: ServiceSpec, replica: int, env: Mapping[str, str]
    ) -> list[str]:
        name = self._name(spec, replica)
        argv = [
            "docker", "run", "-d",
            "--name", name,
            "--label", f"{_LABEL_SANDBOX}=1",
            "--label", f"{_LABEL_SERVICE}={spec.service_id}",
            "--restart", spec.restart_policy,
        ]
        deployment = spec.labels.get("mo.deployment")
        if deployment:
            argv += ["--label", f"{_LABEL_DEPLOYMENT}={deployment}"]
        for key, value in spec.labels.items():
            if key.startswith("mo."):
                continue
            argv += ["--label", f"{key}={value}"]

        for port in spec.ports:
            argv += ["-p", _port_mapping(port)]

        for volume in spec.volumes:
            host = self.host_volume_path(spec, volume["name"])
            # ``:rw`` is Docker's default; stated so a read-only or labelled
            # mount cannot be inferred from a missing field later.
            argv += ["-v", f"{host}:{volume['mount_path']}:rw"]

        if spec.cpus is not None:
            argv += ["--cpus", str(spec.cpus)]
        if spec.memory:
            argv += ["--memory", spec.memory]
        if spec.network:
            argv += ["--network", spec.network]
            for alias in spec.network_aliases:
                argv += ["--network-alias", alias]
        if spec.user:
            argv += ["--user", spec.user]

        hc = spec.healthcheck or {}
        if hc.get("cmd"):
            argv += ["--health-cmd", str(hc["cmd"])]
            if hc.get("interval"):
                argv += ["--health-interval", str(hc["interval"])]
            if hc.get("timeout"):
                argv += ["--health-timeout", str(hc["timeout"])]
            if hc.get("retries") is not None:
                argv += ["--health-retries", str(int(hc["retries"]))]
            if hc.get("start_period"):
                argv += ["--health-start-period", str(hc["start_period"])]

        for key, value in self._env_for(spec, env).items():
            argv += ["-e", f"{key}={value}"]

        argv.append(spec.image)
        argv.extend(spec.command)
        return argv

    # ---- lifecycle ----------------------------------------------------

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        spec, env = _validate(payload)
        replace = bool(payload.get("replace", True))

        if spec.service_id in self._specs and replace:
            self.delete(spec.service_id)

        if spec.service_id in self._specs:
            raise ServiceError(f"service {spec.service_id!r} already exists")

        if spec.network:
            self._ensure_network(spec.network)
        self._prepare_volumes(spec)

        cids: dict[int, str] = {}
        for replica in range(spec.replicas):
            argv = self._docker_run_argv(spec, replica, env)
            result = self._run(argv)
            if int(getattr(result, "returncode", 1)) != 0:
                # Roll back replicas already started so a partial create does
                # not leave a half-scaled service billed to the tenant.
                for started in cids.values():
                    self._run(["docker", "rm", "-f", started])
                stderr = (getattr(result, "stderr", "") or "").strip()
                stdout = (getattr(result, "stdout", "") or "").strip()
                raise RuntimeError(
                    f"docker run failed ({result.returncode}): {stderr or stdout}"
                )
            cids[replica] = (getattr(result, "stdout", "") or "").strip()[:64]

        self._specs[spec.service_id] = spec
        self._cids[spec.service_id] = cids
        self._save()
        return self.status(spec.service_id) or {}

    def delete(self, service_id: str) -> bool:
        """Remove every replica. The service's volume directories stay.

        Deleting a service is a deployment action, not a data-retention
        decision; the platform decides retention (and bills for it). Data
        removal is an explicit, separate act.
        """
        if service_id not in self._specs and service_id not in self._cids:
            return False
        for cid in self._cids.get(service_id, {}).values():
            self._run(["docker", "rm", "-f", cid])
        # Also remove by name: a container whose id was never captured (the
        # previous create crashed) still carries the deterministic name.
        spec = self._specs.get(service_id)
        if spec is not None:
            for replica in range(max(spec.replicas, 1)):
                self._run(["docker", "rm", "-f", self._name(spec, replica)])
        self._specs.pop(service_id, None)
        self._cids.pop(service_id, None)
        self._save()
        return True

    def restart(self, service_id: str) -> dict[str, Any]:
        spec = self._specs.get(service_id)
        if spec is None:
            raise KeyError(service_id)
        for replica in range(spec.replicas):
            self._run(["docker", "restart", self._name(spec, replica)])
        spec.updated_at = time.time()
        self._save()
        return self.status(service_id) or {}

    def logs(self, service_id: str, *, tail: int = 200) -> str:
        spec = self._specs.get(service_id)
        if spec is None:
            raise KeyError(service_id)
        tail = max(1, min(int(tail), 5000))
        chunks: list[str] = []
        for replica in range(spec.replicas):
            result = self._run(
                ["docker", "logs", "--tail", str(tail), self._name(spec, replica)]
            )
            text = (getattr(result, "stdout", "") or "") + (
                getattr(result, "stderr", "") or ""
            )
            chunks.append(f"--- replica {replica} ---\n{text}")
        return "\n".join(chunks)

    def follow_logs(
        self, service_id: str, *, tail: int = 50, replica: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Live log frames for one service, one ``docker logs --follow`` per replica.

        Frames are ``{"replica", "ts", "line"}``; each replica ends with
        ``{"replica", "eof": true, "exit_code"}``. Frames arrive in *arrival*
        order across replicas, not sorted: sorting would mean holding output
        until every replica had spoken, which is the opposite of a live tail.
        ``ts`` is Docker's timestamp, so a client that wants a merged timeline
        can order by it.

        Validation happens here, before the caller starts a response, so an
        unknown service (``KeyError``) or replica (``ServiceError``) is still an
        ordinary HTTP error rather than a half-written stream. The generator
        owns the children: when the reader stops, ``finally`` kills them, so a
        closed browser tab does not leave a Docker process per replica.
        """
        spec = self._specs.get(service_id)
        if spec is None:
            raise KeyError(service_id)
        tail = max(1, min(int(tail), _LOG_TAIL_MAX))
        count = max(spec.replicas, 1)
        if replica is None:
            replicas = list(range(count))
        else:
            try:
                index = int(replica)
            except (TypeError, ValueError) as exc:
                raise ServiceError("replica must be an integer") from exc
            if not 0 <= index < count:
                raise ServiceError(
                    f"replica must be between 0 and {count - 1}, got {index}"
                )
            replicas = [index]
        names = [(index, self._name(spec, index)) for index in replicas]
        return self._follow(names, tail=tail)

    def _follow(
        self, names: list[tuple[int, str]], *, tail: int
    ) -> Iterator[dict[str, Any]]:
        frames: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_LOG_QUEUE_FRAMES)
        stopped = threading.Event()
        procs: list[subprocess.Popen] = []

        def offer(frame: dict[str, Any]) -> bool:
            """Put a frame, giving up if the stream was closed while waiting."""
            while not stopped.is_set():
                try:
                    frames.put(frame, timeout=0.25)
                    return True
                except queue.Full:
                    continue
            return False

        def pump(index: int, proc: subprocess.Popen) -> None:
            try:
                stream = proc.stdout
                if stream is not None:
                    # A line is emitted when Docker writes its newline. A
                    # partial line (a progress bar) waits for one, which is
                    # what ``docker logs`` itself shows.
                    for raw in stream:
                        frame = _parse_log_line(index, raw)
                        if frame is not None and not offer(frame):
                            return
            except (OSError, ValueError):
                pass
            finally:
                try:
                    code = proc.wait()
                except OSError:
                    code = None
                if not stopped.is_set():
                    offer({"replica": index, "eof": True, "exit_code": code})
                stream = proc.stdout
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

        try:
            for index, name in names:
                procs.append(
                    self._popen(
                        [
                            "docker",
                            "logs",
                            "--follow",
                            "--tail",
                            str(tail),
                            "--timestamps",
                            name,
                        ]
                    )
                )
                threading.Thread(
                    target=pump,
                    args=(index, procs[-1]),
                    daemon=True,
                    name=f"service-logs-{index}",
                ).start()
            remaining = len(names)
            while remaining > 0:
                frame = frames.get()
                if frame.get("eof"):
                    remaining -= 1
                yield frame
        finally:
            # Set first: a pump blocked on a full queue must see the close
            # before it tries to push an eof nobody will read.
            stopped.set()
            for proc in procs:
                try:
                    if proc.poll() is None:
                        proc.kill()
                except OSError:
                    pass

    def stats(self, service_id: str) -> dict[str, Any] | None:
        """One ``docker stats --no-stream`` sample for every running replica.

        The I/O counters are reported as Docker keeps them — cumulative since
        the container started — because a rate needs two samples and the node
        cannot know the caller's interval. The control plane stores the
        counters and derives a rate; inventing one here would make the answer
        depend on when the node happened to be asked.

        A stopped replica contributes no entry: ``docker stats`` only measures
        running containers, and reporting zeros would draw a flat line that
        reads as "idle" instead of "not running". ``replicas_total`` says how
        many were asked for, so a missing replica is visible rather than
        inferred.

        A command that fails *and* produced no parseable line raises; one that
        produced some (docker exits non-zero when any named container is not
        running) returns the sample it did get.
        """
        spec = self._specs.get(service_id)
        if spec is None:
            return None
        names = [self._name(spec, index) for index in range(spec.replicas)]
        result = self._run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *names]
        )
        by_name: dict[str, dict[str, Any]] = {}
        for line in (getattr(result, "stdout", "") or "").splitlines():
            parsed = _parse_stats_line(line.strip())
            if parsed is not None:
                by_name[parsed["name"]] = parsed
        if not by_name and int(getattr(result, "returncode", 0)) != 0:
            detail = (getattr(result, "stderr", "") or "").strip()[-500:] or "no output"
            raise RuntimeError(f"docker stats failed: {detail}")
        replicas = [
            {"replica": index, **by_name[name]}
            for index, name in enumerate(names)
            if name in by_name
        ]
        return {
            "service_id": service_id,
            "replicas": replicas,
            "replicas_total": spec.replicas,
            "sampled_at": _utc_now(),
        }

    #: Bounds on a control-plane exec. The node runs an argv it did not write,
    #: so the shape is capped before anything reaches Docker: a caller cannot
    #: stream an unbounded body through the agent or keep a child alive for
    #: hours. Every limit is a refusal, never a truncation of the request.
    _EXEC_MAX_ARGV = 64
    _EXEC_MAX_ARG = 4096
    _EXEC_MAX_STDIN = 1 << 20
    _EXEC_MAX_OUTPUT = 1 << 20
    _EXEC_MAX_TIMEOUT = 300

    def exec_in_service(
        self,
        service_id: str,
        argv: object,
        *,
        stdin: object = "",
        timeout: object = 30,
    ) -> dict[str, Any]:
        """Run one argv inside replica 0 of a service.

        The primitive exists for engine maintenance the platform owns — rotating
        a managed database's password, for instance — not as a tenant shell.
        ``stdin`` is how a value that must not be in argv reaches the command,
        because ``ps`` on the node shows every ``docker exec`` argument. The
        node deliberately logs neither argv nor stdin; the control plane records
        argc and the exit code, which is enough to audit that something ran and
        not enough to leak what it carried.

        Replica 0 is the only target: a managed engine runs one replica, and a
        fan-out would produce an answer the caller has no per-replica use for.
        A container that is not running is refused rather than started: exec is
        maintenance on a live service, not a way to wake one.
        """
        spec = self._specs.get(service_id)
        if spec is None:
            raise KeyError(service_id)
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ServiceError("argv must be a non-empty list of strings")
        if len(argv) > self._EXEC_MAX_ARGV:
            raise ServiceError(f"argv may carry at most {self._EXEC_MAX_ARGV} entries")
        cleaned: list[str] = []
        for arg in argv:
            if not isinstance(arg, str) or arg == "":
                raise ServiceError("argv entries must be non-empty strings")
            if len(arg.encode("utf-8")) > self._EXEC_MAX_ARG:
                raise ServiceError(
                    f"an argv entry exceeds {self._EXEC_MAX_ARG} bytes"
                )
            cleaned.append(arg)
        if not isinstance(stdin, str):
            raise ServiceError("stdin must be a string")
        if len(stdin.encode("utf-8")) > self._EXEC_MAX_STDIN:
            raise ServiceError(f"stdin exceeds {self._EXEC_MAX_STDIN} bytes")
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ServiceError("timeout must be an integer number of seconds")
        if not 1 <= timeout <= self._EXEC_MAX_TIMEOUT:
            raise ServiceError(f"timeout must be between 1 and {self._EXEC_MAX_TIMEOUT}")

        name = self._name(spec, 0)
        state = self._replica_state(name)
        if state != "running":
            raise ServiceError(f"replica {name} is {state}; exec needs a running container")

        try:
            result = self._run_input(
                ["docker", "exec", "-i", name, *cleaned],
                stdin=stdin,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ServiceExecTimeoutError(
                f"exec in {name} did not finish within {timeout}s"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"could not exec in {name}: {exc}") from exc

        def bounded(text: object) -> str:
            value = str(text or "")
            # Keep the tail: an error a command prints last is what explains
            # the exit, and a silent cut is worse than a stated one.
            if len(value) <= self._EXEC_MAX_OUTPUT:
                return value
            return "…" + value[-self._EXEC_MAX_OUTPUT :]

        return {
            "service_id": service_id,
            "replica": 0,
            "exit_code": int(getattr(result, "returncode", 1)),
            "stdout": bounded(getattr(result, "stdout", "")),
            "stderr": bounded(getattr(result, "stderr", "")),
        }

    #: Docker's zero value for ``State.FinishedAt``: a container that has
    #: never finished. Reporting it as a timestamp would be a lie the caller
    #: has to special-case; ``null`` says "still running" and nothing else.
    _NEVER = "0001-01-01T00:00:00Z"

    #: States in which Docker's ``ExitCode`` means something. A *running*
    #: container also reports ``ExitCode: 0`` — passing that through would let
    #: a supervisor read "success" from a job that has not finished, so it is
    #: normalized to ``null`` here and the state stays the authority.
    _TERMINAL_STATES = frozenset({"exited", "dead"})

    def _replica_detail(self, name: str) -> dict[str, Any]:
        """One ``docker inspect`` per replica, returning state and completion.

        ``exit_code`` and ``finished_at`` exist for one-shot workloads: a
        scheduled job's exit code *is* its verdict, and a supervisor that only
        saw ``exited`` could not tell success from a crash. A long-running
        service reports ``exit_code: null`` until it stops.
        """
        result = self._run(
            [
                "docker", "inspect", "-f",
                "{{.State.Status}}|{{.State.ExitCode}}|{{.State.FinishedAt}}",
                name,
            ]
        )
        if int(getattr(result, "returncode", 1)) != 0:
            return {"state": "missing", "exit_code": None, "finished_at": None}
        raw = (getattr(result, "stdout", "") or "").strip()
        parts = raw.split("|", 2)
        state = (parts[0] if parts else "").strip() or "unknown"
        terminal = state in self._TERMINAL_STATES

        exit_code: int | None = None
        if terminal and len(parts) > 1:
            code = parts[1].strip()
            if code.lstrip("-").isdigit():
                exit_code = int(code)

        finished_at: str | None = None
        if terminal and len(parts) > 2:
            stamp = parts[2].strip()
            if stamp and stamp != self._NEVER:
                finished_at = stamp

        return {"state": state, "exit_code": exit_code, "finished_at": finished_at}

    def _replica_state(self, name: str) -> str:
        return str(self._replica_detail(name)["state"])

    def status(self, service_id: str) -> dict[str, Any] | None:
        spec = self._specs.get(service_id)
        if spec is None:
            return None
        replicas = []
        for index in range(spec.replicas):
            name = self._name(spec, index)
            replicas.append(
                {
                    "replica": index,
                    "name": name,
                    "cid": self._cids.get(service_id, {}).get(index),
                    **self._replica_detail(name),
                }
            )
        return {**spec.to_dict(), "replicas_state": replicas}

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for service_id in sorted(self._specs):
            state = self.status(service_id)
            if state is not None:
                out.append(state)
        return out


def make_router(
    state_dir: Path,
    *,
    bearer: Callable,
    volume_root: Path | None = None,
    snapshot_root: Path | None = None,
    snapshot_max_bytes: int | None = None,
    _run_fn: Callable | None = None,
    _run_input_fn: Callable | None = None,
    _popen_fn: Callable[[list[str]], subprocess.Popen] | None = None,
) -> APIRouter:
    """Build the FastAPI router exposing the ``/v1/services`` surface.

    Mirrors :func:`mini_ork.remote.node_agent.images.make_router`: the router
    is additive, every route inherits the bearer dependency, and bodies are
    ``dict`` with manual validation so a missing field is a 400 (not the 422
    FastAPI would infer from a Pydantic model).

    ``volume_root`` and ``snapshot_root`` are resolved when the manager is
    built — that is, at startup — so a misconfigured ``MO_NODE_VOLUME_ROOT`` or
    ``MO_NODE_SNAPSHOT_ROOT`` stops the node-agent from serving rather than
    surfacing on the first deploy or backup that needed it.
    """
    services = ServiceManager(
        state_dir,
        volume_root=volume_root,
        snapshot_root=snapshot_root,
        snapshot_max_bytes=snapshot_max_bytes,
        _run_fn=_run_fn,
        _run_input_fn=_run_input_fn,
        _popen_fn=_popen_fn,
    )
    router = APIRouter()

    @router.post("/v1/services")
    def post_service(payload: dict, _: None = Depends(bearer)) -> dict:
        try:
            return services.create(payload)
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc

    @router.get("/v1/services")
    def list_services(_: None = Depends(bearer)) -> dict:
        return {"services": services.list()}

    @router.get("/v1/services/{sid}")
    def get_service(sid: str, _: None = Depends(bearer)) -> dict:
        state = services.status(sid)
        if state is None:
            raise HTTPException(404, f"unknown service {sid!r}")
        return state

    @router.delete("/v1/services/{sid}")
    def delete_service(sid: str, _: None = Depends(bearer)) -> dict:
        if not services.delete(sid):
            raise HTTPException(404, f"unknown service {sid!r}")
        return {"deleted": sid}

    @router.post("/v1/services/{sid}/restart")
    def restart_service(sid: str, _: None = Depends(bearer)) -> dict:
        try:
            return services.restart(sid)
        except KeyError as exc:
            raise HTTPException(404, f"unknown service {sid!r}") from exc

    @router.get("/v1/services/{sid}/logs")
    def get_service_logs(sid: str, tail: int = 200,
                         _: None = Depends(bearer)) -> dict:
        try:
            return {"service_id": sid, "logs": services.logs(sid, tail=tail)}
        except KeyError as exc:
            raise HTTPException(404, f"unknown service {sid!r}") from exc

    @router.get("/v1/services/{sid}/logs/stream")
    def stream_service_logs(sid: str, tail: int = 50, replica: int | None = None,
                            _: None = Depends(bearer)) -> Response:
        """Live tail as NDJSON: ``{"replica","ts","line"}`` then ``{"eof":true}``.

        ``follow_logs`` validates before this returns a generator, so an
        unknown service or a replica outside the range is still a normal JSON
        error — a stream that starts and then reports 404 has already told the
        client it succeeded.
        """
        try:
            frames = services.follow_logs(sid, tail=tail, replica=replica)
        except KeyError as exc:
            raise HTTPException(404, f"unknown service {sid!r}") from exc
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

        def gen() -> Iterator[str]:
            for frame in frames:
                yield json.dumps(frame) + "\n"

        return StreamingResponse(
            gen(),
            media_type="application/x-ndjson",
            headers={
                # A tail is never cacheable, and a buffering edge would turn
                # live output into one delayed blob at EOF.
                "cache-control": "no-store",
                "x-accel-buffering": "no",
            },
        )

    @router.get("/v1/services/{sid}/stats")
    def get_service_stats(sid: str, _: None = Depends(bearer)) -> dict:
        try:
            sample = services.stats(sid)
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        if sample is None:
            raise HTTPException(404, f"unknown service {sid!r}")
        return sample

    @router.post("/v1/services/{sid}/exec")
    def post_service_exec(sid: str, payload: dict | None = None,
                          _: None = Depends(bearer)) -> dict:
        body = payload or {}
        try:
            return services.exec_in_service(
                sid,
                body.get("argv"),
                stdin=body.get("stdin", ""),
                timeout=body.get("timeout", 30),
            )
        except KeyError as exc:
            raise HTTPException(404, f"unknown service {sid!r}") from exc
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ServiceExecTimeoutError as exc:
            raise HTTPException(504, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc

    # ---- volume snapshots ---------------------------------------------

    @router.post("/v1/services/{sid}/volumes/{name}/snapshots")
    def post_volume_snapshot(sid: str, name: str, payload: dict | None = None,
                             _: None = Depends(bearer)) -> dict:
        body = payload or {}
        quiesce = body.get("quiesce", False)
        if not isinstance(quiesce, bool):
            # A string "false" is truthy; refusing the shape is how a caller
            # that meant a crash-consistent copy does not get a stopping one.
            raise HTTPException(400, "quiesce must be a boolean")
        try:
            return services.snapshot(sid, name, quiesce=quiesce)
        except VolumeNotMaterializedError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc

    @router.get("/v1/services/{sid}/volumes/{name}/snapshots")
    def list_volume_snapshots(sid: str, name: str, _: None = Depends(bearer)) -> dict:
        try:
            snapshots = services.list_snapshots(sid, name)
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"snapshots": snapshots, "count": len(snapshots)}

    @router.delete("/v1/services/{sid}/volumes/{name}/snapshots/{snap}")
    def delete_volume_snapshot(sid: str, name: str, snap: str,
                               _: None = Depends(bearer)) -> dict:
        try:
            services.delete_snapshot(sid, name, snap)
        except SnapshotNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"deleted": snap}

    @router.get("/v1/services/{sid}/volumes/{name}/snapshots/{snap}/blob")
    def get_snapshot_blob(sid: str, name: str, snap: str,
                          _: None = Depends(bearer)) -> FileResponse:
        try:
            archive, meta = services.snapshot_archive(sid, name, snap)
        except SnapshotNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(
            archive,
            media_type="application/gzip",
            filename=f"{snap}.tar.gz",
            headers={
                # The control plane records what it shipped; these headers make
                # the bytes self-describing so it can verify the download without
                # a second metadata call that could race a delete.
                _SNAPSHOT_SHA256_HEADER: str(meta.get("sha256") or ""),
                "x-content-bytes": str(meta.get("bytes") or ""),
            },
        )

    @router.put("/v1/services/{sid}/volumes/{name}/snapshots/{snap}/blob")
    async def put_snapshot_blob(sid: str, name: str, snap: str, request: Request,
                                _: None = Depends(bearer)) -> dict:
        declared = request.headers.get(_SNAPSHOT_SHA256_HEADER, "")
        if not _SHA256_RE.match(declared):
            raise HTTPException(
                400,
                f"{_SNAPSHOT_SHA256_NAME} header is required: 64 lowercase hex characters",
            )
        # Refuse an oversized upload from its declared length before reading a
        # byte; the stream is still counted below, because a chunked body or a
        # lying Content-Length must not become an unbounded write.
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > services.snapshot_max_bytes:
                    raise HTTPException(
                        413,
                        f"snapshot body exceeds {services.snapshot_max_bytes} bytes "
                        f"({_SNAPSHOT_MAX_BYTES_ENV})",
                    )
            except ValueError:
                raise HTTPException(400, "Content-Length must be an integer") from None
        try:
            return await services.import_snapshot(
                sid, name, snap, request.stream(), sha256=declared
            )
        except SnapshotTooLargeError as exc:
            raise HTTPException(413, str(exc)) from exc
        except SnapshotExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except SnapshotNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc

    @router.post("/v1/services/{sid}/volumes/{name}/restore")
    def post_volume_restore(sid: str, name: str, payload: dict,
                            _: None = Depends(bearer)) -> dict:
        try:
            return services.restore(sid, name, payload.get("snapshot_id"))
        except SnapshotNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except VolumeInUseError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"restore failed: {exc}") from exc

    return router


__all__ = [
    "ServiceError",
    "ServiceManager",
    "ServiceSpec",
    "SnapshotExistsError",
    "SnapshotNotFoundError",
    "SnapshotTooLargeError",
    "VolumeInUseError",
    "VolumeNotMaterializedError",
    "make_router",
]
