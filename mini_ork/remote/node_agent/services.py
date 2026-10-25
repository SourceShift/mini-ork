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
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException

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


class ServiceError(ValueError):
    """A request the node refuses before it touches Docker."""


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
        container = int(raw.get("container") or 0)
        if container < 1 or container > 65535:
            raise ServiceError("port.container must be 1..65535")
        host_raw = raw.get("host")
        host = int(host_raw) if host_raw is not None else None
        if host is not None and (host < 1 or host > 65535):
            raise ServiceError("port.host must be 1..65535")
        ports.append({"container": container, "host": host})

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
        network=str(payload["network"]) if payload.get("network") else None,
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
        _run_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.volume_root = self._resolve_volume_root(volume_root)
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
            host = port.get("host")
            mapping = f"{host}:{port['container']}" if host else f"{port['container']}"
            argv += ["-p", mapping]

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

    def _replica_state(self, name: str) -> str:
        result = self._run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name]
        )
        if int(getattr(result, "returncode", 1)) != 0:
            return "missing"
        return (getattr(result, "stdout", "") or "").strip() or "unknown"

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
                    "state": self._replica_state(name),
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
    _run_fn: Callable | None = None,
) -> APIRouter:
    """Build the FastAPI router exposing the ``/v1/services`` surface.

    Mirrors :func:`mini_ork.remote.node_agent.images.make_router`: the router
    is additive, every route inherits the bearer dependency, and bodies are
    ``dict`` with manual validation so a missing field is a 400 (not the 422
    FastAPI would infer from a Pydantic model).

    ``volume_root`` is resolved when the manager is built — that is, at startup
    — so a misconfigured ``MO_NODE_VOLUME_ROOT`` stops the node-agent from
    serving rather than surfacing on the first deploy that needed a volume.
    """
    services = ServiceManager(state_dir, volume_root=volume_root, _run_fn=_run_fn)
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

    return router


__all__ = ["ServiceError", "ServiceManager", "ServiceSpec", "make_router"]
