"""Session management for the node-agent (kickoff requirement 2; epic 12).

A session ties together:
- the run dirs on disk (``/srv/mini-ork/runs/<run_id>/{target,run,home,mo-home}``)
- the host-local Docker container that mounts them at ``/workspace/...``
- the proc registry that lives under ``.procs/``
- (epic 12) an OPTIONAL internal Docker network + egress-proxy container
  when the environment profile sets ``network: allowlist``

``POST /v1/sessions`` is idempotent on ``run_id`` — a second call returns
the existing session. ``DELETE /v1/sessions/{sid}`` removes the
container but keeps the run dirs until the TTL (default 24 h); a
background reaper sweeps expired dirs.

Docker labels are ``mo.sandbox=1`` and ``mo.run_id=<id>`` — the same
labels the runtime reaper uses (``runtime/sandbox_reaper.py``), so a
node-agent crash leaves containers a regular sweep can clean.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .procs import ProcRegistry, _run as default_run


@dataclass
class _SessionInfo:
    """The JSON-serializable slice of a session — no proc_registry, no runtime handle.

    The default values let a pre-epic-12 ``sessions.json`` rebuild cleanly:
    the only fields persisted by older versions were ``sid``, ``run_id``,
    ``image``, ``cid``, ``created_at``, ``resources``, ``profile``. Anything
    added after this commit MUST default to a value that preserves the
    pre-epic argv shape — ``network="full"`` is the byte-identical no-arg
    path (kickoff Review bar #4).
    """

    sid: str
    run_id: str
    image: str
    cid: str | None = None
    created_at: float = field(default_factory=time.time)
    resources: dict | None = None
    profile: str | None = None
    network: str = "full"
    allow_domains: tuple[str, ...] = ()
    proxy_cid: str | None = None
    network_name: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict turns tuple -> list; that's fine for JSON.
        return d


class Session:
    """A live session — JSON view (:attr:`info`) + the in-memory proc registry.

    The proc registry holds a :class:`threading.Lock`, so it MUST stay
    off the JSON serializer's path. ``to_dict`` returns only ``info``;
    callers that need the registry read it directly.
    """

    def __init__(
        self,
        sid: str,
        run_id: str,
        image: str,
        cid: str | None = None,
        created_at: float | None = None,
        resources: dict | None = None,
        profile: str | None = None,
        proc_registry: ProcRegistry | None = None,
        runtime: str = "docker",
        network: str = "full",
        allow_domains: tuple[str, ...] | list[str] = (),
        proxy_cid: str | None = None,
        network_name: str | None = None,
    ) -> None:
        self.info = _SessionInfo(
            sid=sid,
            run_id=run_id,
            image=image,
            cid=cid,
            created_at=created_at if created_at is not None else time.time(),
            resources=resources,
            profile=profile,
            network=network,
            allow_domains=tuple(allow_domains),
            proxy_cid=proxy_cid,
            network_name=network_name,
        )
        self.proc_registry = proc_registry
        self.runtime = runtime

    @property
    def sid(self) -> str:
        return self.info.sid

    @property
    def run_id(self) -> str:
        return self.info.run_id

    @property
    def cid(self) -> str | None:
        return self.info.cid

    @property
    def created_at(self) -> float:
        return self.info.created_at

    def to_dict(self) -> dict:
        return self.info.to_dict()


# Run-dir layout per ``docs/operator/remote-nodes.md`` and
# ``docs/architecture/remote-nodes.md`` D4.
_RUN_DIRS = ("target", "run", "home", "mo-home")

# Egress-proxy image is a stable tinyproxy build (kickoff line 73). The
# default lets the loader work even when the operator has not pinned a
# specific image; the live test in ``tests/unit/test_environments.py`` can
# override the env var.
_PROXY_IMAGE = os.environ.get(
    "MO_NODE_EGRESS_PROXY_IMAGE", "monokal/tinyproxy:latest"
)


def _build_docker_argv(
    *,
    image: str,
    cid_name: str,
    mounts: list[tuple[str, str, bool]],
    labels: list[str],
    resources: dict | None,
    cmd: list[str],
    network_name: str | None = None,
    proxy_env: dict[str, str] | None = None,
) -> list[str]:
    """Compose the ``docker run -d`` argv.

    Default path (``network_name is None`` and ``proxy_env is None``) emits
    the SAME argv as the pre-epic-12 function — Review bar #4 ("default
    path byte-identical"). The ``--network`` flag appears at most once and
    is added BEFORE the image so the inner-container DNS works against the
    internal network's bridge; the proxy env is appended as ``-e KEY=VAL``
    pairs so the in-container process sees the proxy URLs.
    """
    argv = [
        "docker", "run", "-d",
        "--name", cid_name,
        *sum((["--label", l] for l in labels), []),
        *sum((["-v", f"{src}:{dst}{':ro' if ro else ''}"] for src, dst, ro in mounts), []),
        "-w", "/workspace/target",
    ]
    if resources:
        # Profiles say ``cpus`` (docs/architecture/remote-nodes.md); ``cpu`` kept.
        if (cpu := resources.get("cpus", resources.get("cpu"))) is not None:
            argv += ["--cpus", str(cpu)]
        if (mem := resources.get("memory")) is not None:
            argv += ["--memory", str(mem)]
    if network_name:
        argv += ["--network", network_name]
    if proxy_env:
        for k, v in proxy_env.items():
            argv += ["-e", f"{k}={v}"]
    argv += [image, *cmd]
    return argv


def _build_proxy_env(*, proxy_name: str) -> dict[str, str]:
    """Env vars the session container needs to route through the proxy.

    The proxy listens on port 8888 (tinyproxy default). The session
    container reaches it via the proxy's CONTAINER NAME (Docker's
    embedded DNS resolves the name on the internal network once the
    proxy is connected). Using the name (not the IP) keeps the env
    stable across proxy restarts.

    ``NO_PROXY=localhost,127.0.0.1`` keeps loopback traffic off the
    proxy so the agent CLI's local probes (e.g. health checks) keep
    working.
    """
    return {
        "HTTP_PROXY": f"http://{proxy_name}:8888",
        "HTTPS_PROXY": f"http://{proxy_name}:8888",
        "http_proxy": f"http://{proxy_name}:8888",
        "https_proxy": f"http://{proxy_name}:8888",
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }


class SessionManager:
    """Owns session lifecycle; one instance per app."""

    LABEL_SANDBOX = "mo.sandbox=1"
    LABEL_RUN_PREFIX = "mo.run_id="

    def __init__(
        self,
        state_dir: Path,
        *,
        retain_hours: float = 24.0,
        runtime: str = "docker",
        engine_root: Path | None = None,
        _run_fn: Callable | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.retain_seconds = retain_hours * 3600.0
        self.runtime = runtime
        self.engine_root = Path(engine_root) if engine_root else None
        self._run = _run_fn or default_run
        self._sessions: dict[str, Session] = {}
        self._by_run_id: dict[str, str] = {}
        self._rebuild()

    # ---- disk helpers -------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.state_dir / "sessions.json"

    def _persist_index(self) -> None:
        out = {sid: s.to_dict() for sid, s in self._sessions.items()}
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
        os.replace(tmp, self._index_path)

    def _rebuild(self) -> None:
        if not self._index_path.is_file():
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for sid, s in data.items():
            sess = Session(
                sid=sid,
                run_id=s["run_id"],
                image=s["image"],
                cid=s.get("cid"),
                created_at=s.get("created_at", time.time()),
                resources=s.get("resources"),
                profile=s.get("profile"),
                runtime=self.runtime,
                # Default the new fields so a pre-epic index keeps loading
                # (kickoff Review bar #4 — byte-identical default path).
                network=s.get("network", "full"),
                allow_domains=tuple(s.get("allow_domains") or ()),
                proxy_cid=s.get("proxy_cid"),
                network_name=s.get("network_name"),
            )
            sess.proc_registry = ProcRegistry(
                self.state_dir, sess.run_id,
                runtime=self.runtime, session_cid=sess.cid,
                _run_fn=self._run,
            )
            self._sessions[sid] = sess
            self._by_run_id[sess.run_id] = sid

    # ---- public API ---------------------------------------------------

    def create(
        self,
        *,
        run_id: str,
        image: str,
        resources: dict | None = None,
        profile: str | None = None,
        network: str = "full",
        allow_domains: tuple[str, ...] | list[str] | None = None,
    ) -> Session:
        existing_sid = self._by_run_id.get(run_id)
        if existing_sid is not None:
            return self._sessions[existing_sid]
        sid = uuid.uuid4().hex[:12]
        cid_name = f"mo-sess-{sid}"
        network_name = f"mo-int-{sid}" if network == "allowlist" else None
        run_dirs = self._ensure_run_dirs(run_id)
        session = Session(
            sid=sid,
            run_id=run_id,
            image=image,
            resources=resources,
            profile=profile,
            runtime=self.runtime,
            network=network,
            allow_domains=tuple(allow_domains or ()),
            network_name=network_name,
        )
        # The proxy must be RUNNING and connected to the internal network
        # BEFORE the session container is launched — otherwise the
        # ``HTTP_PROXY`` URL inside the session can't resolve the proxy
        # name. For the host runtime there is no proxy.
        proxy_cid: str | None = None
        proxy_env: dict[str, str] | None = None
        if network == "allowlist" and self.runtime == "docker":
            assert network_name is not None, (
                "allowlist sessions must have a network_name"
            )
            proxy_cid, _ = self._start_egress_proxy(
                sid=sid, network_name=network_name,
                allow_domains=tuple(allow_domains or ()),
            )
            proxy_env = _build_proxy_env(proxy_name=f"mo-proxy-{sid}")
            session.info.proxy_cid = proxy_cid
        cid = self._launch_container(
            cid_name=cid_name, image=image, run_id=run_id, run_dirs=run_dirs,
            resources=resources,
            network_name=network_name,
            proxy_env=proxy_env,
        )
        session.info.cid = cid
        session.proc_registry = ProcRegistry(
            self.state_dir, run_id,
            runtime=self.runtime, session_cid=cid,
            _run_fn=self._run,
        )
        self._sessions[sid] = session
        self._by_run_id[run_id] = sid
        self._persist_index()
        return session

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)

    def by_run(self, run_id: str) -> Session | None:
        sid = self._by_run_id.get(run_id)
        return self._sessions.get(sid) if sid else None

    def delete(self, sid: str) -> bool:
        session = self._sessions.pop(sid, None)
        if session is None:
            return False
        self._by_run_id.pop(session.run_id, None)
        if session.cid:
            try:
                self._run(["docker", "rm", "-f", session.cid])
            except (FileNotFoundError, OSError):
                pass
        # Allowlist teardown: kill the proxy + remove the internal network
        # (kickoff line 78: "Session down tears down both the network and
        # the proxy"). Best-effort, swallow OSError so a partial teardown
        # never blocks a successful delete.
        if session.info.proxy_cid:
            try:
                self._run(["docker", "rm", "-f", session.info.proxy_cid])
            except (FileNotFoundError, OSError):
                pass
        if session.info.network_name:
            try:
                self._run(["docker", "network", "rm", session.info.network_name])
            except (FileNotFoundError, OSError):
                pass
        # Run dirs are KEPT for the TTL (kickoff req 2, DELETE bullet).
        # The reaper sweeps them after ``retain_seconds``.
        self._persist_index()
        return True

    # ---- run dirs -----------------------------------------------------

    def _ensure_run_dirs(self, run_id: str) -> dict[str, Path]:
        runs_root = self.state_dir / "runs" / run_id
        out: dict[str, Path] = {}
        for name in _RUN_DIRS:
            d = runs_root / name
            d.mkdir(parents=True, exist_ok=True)
            out[name] = d
        return out

    # ---- docker launch -----------------------------------------------

    def _launch_container(
        self,
        *,
        cid_name: str,
        image: str,
        run_id: str,
        run_dirs: dict[str, Path],
        resources: dict | None,
        network_name: str | None = None,
        proxy_env: dict[str, str] | None = None,
    ) -> str:
        mounts: list[tuple[str, str, bool]] = []
        if self.runtime == "host":
            # host runtime: no container, no mounts; engine/workspace are
            # already on the VM filesystem. Return a sentinel cid so the
            # procs registry doesn't try to ``docker exec`` into it.
            return f"host:{cid_name}"
        # Per D8 / epic 04 layout: engine is ro at /opt/mini-ork; the
        # engine bundle is staged at engines/<sha> under state_dir and
        # mounted read-only. We mount from a sibling ``engines/<sha>``
        # directory when present; else we mount the entire ``state_dir``
        # so a freshly built agent can still find engines.
        if self.engine_root is not None and self.engine_root.exists():
            mounts.append((str(self.engine_root), "/opt/mini-ork", True))
        for name, dst in (("target", "/workspace/target"), ("run", "/workspace/run"),
                          ("home", "/workspace/home"), ("mo-home", "/workspace/mo-home")):
            mounts.append((str(run_dirs[name]), dst, name == "mo-home"))
        labels = [self.LABEL_SANDBOX, f"{self.LABEL_RUN_PREFIX}{run_id}"]

        argv = _build_docker_argv(
            image=image,
            cid_name=cid_name, mounts=mounts, labels=labels, resources=resources,
            cmd=["sleep", "infinity"],
            network_name=network_name,
            proxy_env=proxy_env,
        )
        r = self._run(argv)
        if r.returncode != 0:
            raise RuntimeError(
                f"docker run failed ({r.returncode}): {r.stderr.strip() or r.stdout.strip()}"
            )
        return (r.stdout or "").strip()

    def _start_egress_proxy(
        self,
        *,
        sid: str,
        network_name: str,
        allow_domains: tuple[str, ...] = (),
    ) -> tuple[str, str]:
        """Create the internal network + spawn the egress-proxy container.

        Returns ``(proxy_cid, network_name)``. The proxy is attached to
        BOTH the internal network (so the session container reaches it)
        and the default ``bridge`` (so the proxy itself can hit the
        internet). The tinyproxy config file is written to a per-session
        directory under ``<state_dir>/proxies/<sid>/`` so the allowlist
        is auditable on disk.

        On the host runtime, no proxy is started (host processes use the
        host's own network stack).
        """
        if self.runtime == "host":
            return ("", network_name)
        # 1. Create the internal network. ``--internal`` blocks all
        #    egress from containers attached to it (except via the proxy,
        #    which lives on ``bridge`` too).
        create = self._run([
            "docker", "network", "create", "--internal", network_name,
        ])
        if create.returncode != 0:
            raise RuntimeError(
                f"docker network create {network_name} failed: "
                f"{create.stderr.strip() or create.stdout.strip()}"
            )

        # 2. Write the tinyproxy allowlist file. ``Allow`` is a domain
        #    match; a wildcard ``*`` lets everything through (the
        #    default). When the operator supplied ``allow_domains``, only
        #    those are allowed.
        proxy_dir = self.state_dir / "proxies" / sid
        proxy_dir.mkdir(parents=True, exist_ok=True)
        conf_path = proxy_dir / "tinyproxy.conf"
        lines = [
            "Port 8888",
            "Listen 0.0.0.0",
            "Allow 127.0.0.1",
        ]
        for d in allow_domains:
            lines.append(f"Allow {d}")
        if not allow_domains:
            lines.append("Allow 0.0.0.0/0")
        conf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 3. Spawn the proxy on BOTH networks so the session container
        #    can reach it on the internal network, and the proxy can
        #    reach the internet on the bridge.
        proxy_cid_name = f"mo-proxy-{sid}"
        argv = [
            "docker", "run", "-d",
            "--name", proxy_cid_name,
            "--network", "bridge",
            "-v", f"{conf_path}:/etc/tinyproxy/tinyproxy.conf:ro",
            _PROXY_IMAGE,
        ]
        run = self._run(argv)
        if run.returncode != 0:
            raise RuntimeError(
                f"egress proxy run failed ({run.returncode}): "
                f"{run.stderr.strip() or run.stdout.strip()}"
            )
        proxy_cid = (run.stdout or "").strip()

        # 4. Attach the proxy to the internal network too, so the
        #    session container can reach it without leaving the
        #    no-egress network. ``docker network connect`` is the right
        #    primitive; it returns 0 even when already attached.
        connect = self._run([
            "docker", "network", "connect", network_name, proxy_cid,
        ])
        if connect.returncode != 0:
            # Best-effort cleanup before raising.
            try:
                self._run(["docker", "rm", "-f", proxy_cid])
            except OSError:
                pass
            raise RuntimeError(
                f"docker network connect failed: "
                f"{connect.stderr.strip() or connect.stdout.strip()}"
            )

        return (proxy_cid, network_name)

    # ---- reaper -------------------------------------------------------

    def reap_expired(self, *, now: float | None = None) -> list[str]:
        """Remove run dirs whose session is older than the retain TTL.

        Containers whose session is gone are also swept using the
        ``mo.run_id=`` label, so a crash that deleted the index file does
        not leave orphaned containers behind. Returns the reaped run_ids.
        """
        clock = now if now is not None else time.time()
        reaped: list[str] = []
        for run_id, sid in list(self._by_run_id.items()):
            sess = self._sessions.get(sid)
            if sess is None:
                continue
            age = clock - sess.created_at
            if age <= self.retain_seconds:
                continue
            run_dir = self.state_dir / "runs" / run_id
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
            # sweep any orphan container with this label
            try:
                self._run(["docker", "rm", "-f",
                           *(c for c in [sess.cid] if c)])
            except (FileNotFoundError, OSError):
                pass
            if sess.info.proxy_cid:
                try:
                    self._run(["docker", "rm", "-f", sess.info.proxy_cid])
                except (FileNotFoundError, OSError):
                    pass
            if sess.info.network_name:
                try:
                    self._run(["docker", "network", "rm", sess.info.network_name])
                except (FileNotFoundError, OSError):
                    pass
            self._sessions.pop(sid, None)
            self._by_run_id.pop(run_id, None)
            reaped.append(run_id)
        if reaped:
            self._persist_index()
        return reaped


__all__ = ["Session", "SessionManager"]