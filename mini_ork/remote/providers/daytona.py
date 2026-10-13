"""Daytona node provider: one Daytona sandbox per run, a node-agent inside it.

``provider: daytona`` in an environment profile makes ``--placement remote``
create a Daytona sandbox for the run, boot ``mini-ork node-agent --runtime
host`` inside it from the control plane's own engine, and reach it through
Daytona's authenticated preview proxy. From then on the run talks the ordinary
node-agent protocol, so tree sync, the run-dir mirror, reattach and per-lane
secret scoping work exactly as they do against a self-hosted VM
(``docs/architecture/cloud-sandbox-providers.md``).

Boot, once per sandbox:

1. ``create`` a sandbox named ``mo-<run_id>``. Names are unique per Daytona
   organization, so a lost run-dir record still finds the same sandbox. It
   carries the ``mo.*`` labels and a wall-clock ``ttl_minutes``, so Daytona
   destroys it even if this machine dies.
2. Upload ``git archive HEAD`` of the engine into the node-agent's engine store
   (``<state>/engines/<sha>``). The node-agent then lists that sha in
   ``/v1/health``, and the remote workspace skips its own engine upload.
3. Install whichever node-agent dependencies the image lacks (the default
   snapshot only lacks ``fastapi``), then run the profile's ``setup`` script.
4. Start the node-agent on ``127.0.0.1:7091`` with a freshly minted bearer
   token. The token is kept in a 0600 file in the sandbox and passed only into
   the node-agent's own environment. Spawned agents never inherit it (the
   node-agent builds their env from ``PATH`` plus the request). Nothing on this
   machine's disk ever holds it: ``attach`` re-reads it through Daytona's
   authenticated file API.
5. Wait for ``/v1/health`` through the preview URL.

Every SDK call goes through :func:`_sdk` / :func:`_client`, so ``daytona`` is
imported only when this provider is used. A missing SDK, a missing
``DAYTONA_API_KEY`` or a refused API call is a :class:`ProviderUnavailableError`
(→ ``failure_class=remote_unavailable``), never a silent host fallback.
"""
from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mini_ork.remote.providers.base import (
    NodeEndpoint,
    ProviderUnavailableError,
    register_node_provider,
    resolve_provider_secret,
)

__all__ = ["DaytonaOptions", "DaytonaProvider", "NAME", "sandbox_name"]

NAME = "daytona"
NODE_PORT = 7091
PREVIEW_TOKEN_HEADER = "x-daytona-preview-token"
# Daytona serves an interstitial warning page to browsers on preview URLs; an
# API client must never get HTML where it expects JSON.
_SKIP_WARNING_HEADER = "X-Daytona-Skip-Preview-Warning"
LABEL_SANDBOX = "mo.sandbox"
LABEL_PROVIDER = "mo.provider"
LABEL_RUN = "mo.run"
LABEL_CREATED = "mo.created"
_NODE_DIR = ".mini-ork-node"          # under the sandbox user's home
_SESSION_PREFIX = "mo-node-agent"
_DEFAULT_TTL_MIN = 360               # = sandbox_reaper.DEFAULT_MAX_AGE_S
# import name → pip requirement; what the node-agent itself needs to start.
_NODE_AGENT_DEPS = (("fastapi", "fastapi"), ("uvicorn", "uvicorn[standard]"),
                    ("yaml", "pyyaml"), ("jsonschema", "jsonschema"))


def sandbox_name(run_id: str) -> str:
    """The deterministic sandbox name for a run (Daytona's idempotency key)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    return f"mo-{slug}"[:63].rstrip("-")


def _memory_gib(raw: Any) -> int | None:
    """``4``, ``"4g"``, ``"4096m"`` → GiB (rounded up, minimum 1)."""
    if raw in (None, ""):
        return None
    text = str(raw).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([gm]i?b?)?", text)
    if not m:
        raise ProviderUnavailableError(f"daytona: cannot read memory size {raw!r} "
                                       "(use e.g. 4, '4g' or '4096m')")
    value = float(m.group(1))
    if (m.group(2) or "g").startswith("m"):
        value /= 1024
    return max(1, int(-(-value // 1)))


@dataclass(frozen=True)
class DaytonaOptions:
    """Everything a sandbox is created with, resolved from the profile.

    ``provider_options`` keys (all optional): ``snapshot``, ``target``,
    ``api_url``, ``ttl_minutes``, ``auto_stop_minutes``, ``python``,
    ``bootstrap`` (install missing node-agent deps, default true),
    ``create_timeout_s``, ``ready_timeout_s``.
    """

    snapshot: str = ""
    image: str = ""
    target: str = ""
    api_url: str = ""
    ttl_minutes: int = _DEFAULT_TTL_MIN
    auto_stop_minutes: int = 0
    cpu: int | None = None
    memory_gib: int | None = None
    disk_gib: int | None = None
    python: str = "python3"
    bootstrap: bool = True
    setup: str = ""
    setup_timeout_s: float = 900.0
    env: Mapping[str, str] = field(default_factory=dict)
    domain_allow_list: str = ""
    create_timeout_s: float = 300.0
    ready_timeout_s: float = 180.0

    _KNOWN = frozenset({"snapshot", "target", "api_url", "ttl_minutes", "auto_stop_minutes",
                        "python", "bootstrap", "create_timeout_s", "ready_timeout_s"})

    @classmethod
    def resolve(cls, profile: Any, env: Mapping[str, str]) -> "DaytonaOptions":
        opts = dict(getattr(profile, "provider_options", None) or {})
        unknown = sorted(set(opts) - cls._KNOWN)
        if unknown:
            raise ProviderUnavailableError(
                f"daytona: unknown provider_options {unknown}; known: {sorted(cls._KNOWN)}")
        max_age = (env.get("MO_SANDBOX_MAX_AGE") or "").strip()
        ttl_default = max(1, int(max_age) // 60) if max_age.isdigit() else _DEFAULT_TTL_MIN
        resources = dict(getattr(profile, "resources", None) or {})
        network = str(getattr(profile, "network", "") or "full")
        domains = [str(d) for d in (getattr(profile, "allow_domains", None) or [])]
        if network == "allowlist" and not domains:
            raise ProviderUnavailableError(
                "daytona: network: allowlist needs allow_domains (an empty list would "
                "block the LLM APIs and pip)")
        cpus = resources.get("cpus")
        disk = resources.get("disk")
        return cls(
            snapshot=str(opts.get("snapshot") or env.get("MO_DAYTONA_SNAPSHOT") or ""),
            image=str(getattr(profile, "image", "") or ""),
            target=str(opts.get("target") or env.get("DAYTONA_TARGET") or ""),
            api_url=str(opts.get("api_url") or env.get("DAYTONA_API_URL") or ""),
            ttl_minutes=int(opts.get("ttl_minutes") or ttl_default),
            auto_stop_minutes=int(opts.get("auto_stop_minutes") or 0),
            cpu=int(-(-float(cpus) // 1)) if cpus not in (None, "") else None,
            memory_gib=_memory_gib(resources.get("memory")),
            disk_gib=_memory_gib(disk) if disk not in (None, "") else None,
            python=str(opts.get("python") or "python3"),
            bootstrap=bool(opts.get("bootstrap", True)),
            setup=str(getattr(profile, "setup", "") or ""),
            setup_timeout_s=float(getattr(profile, "setup_timeout_s", 900.0) or 900.0),
            env={str(k): str(v) for k, v in (getattr(profile, "env", None) or {}).items()},
            domain_allow_list=",".join(domains) if network == "allowlist" else "",
            create_timeout_s=float(opts.get("create_timeout_s") or 300.0),
            ready_timeout_s=float(opts.get("ready_timeout_s") or 180.0),
        )


# --- SDK seams ------------------------------------------------------------------


def _sdk() -> Any:
    try:
        import daytona  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderUnavailableError(
            "daytona: the Daytona SDK is not installed — pip install 'mini-ork[daytona]'"
        ) from exc
    return daytona


def _client(env: Mapping[str, str], opts: DaytonaOptions | None = None) -> Any:
    sdk = _sdk()
    key = resolve_provider_secret("DAYTONA_API_KEY", env, provider=NAME)
    api_url = (opts.api_url if opts else "") or (env.get("DAYTONA_API_URL") or "").strip()
    target = (opts.target if opts else "") or (env.get("DAYTONA_TARGET") or "").strip()
    return sdk.Daytona(sdk.DaytonaConfig(api_key=key, api_url=api_url or None,
                                         target=target or None))


def _is(exc: BaseException, name: str) -> bool:
    """``isinstance`` against an SDK error class by name (the SDK is optional)."""
    return any(cls.__name__ == name for cls in type(exc).__mro__)


def _unavailable(action: str, exc: BaseException) -> ProviderUnavailableError:
    return ProviderUnavailableError(f"daytona: {action} failed: {type(exc).__name__}: {exc}")


def _http_ok(url: str, headers: Mapping[str, str], timeout: float = 10.0) -> bool:
    """One GET; True on 2xx. The single network seam besides the SDK (tests fake it)."""
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _engine_archive(engine_root: str) -> tuple[str, bytes]:
    """``(HEAD sha, git archive HEAD as tar.gz)`` of the control plane's engine.

    The same tree the remote workspace stages through its engine bundle (HEAD's
    tree under HEAD's sha), so the node-agent reports the sha the control plane
    expects and the second upload is skipped."""
    def git(*args: str, text: bool = True) -> Any:
        r = subprocess.run(["git", "-C", engine_root, *args], capture_output=True,
                           text=text, check=False, timeout=300)
        if r.returncode != 0:
            err = r.stderr if text else r.stderr.decode("utf-8", "replace")
            raise ProviderUnavailableError(
                f"daytona: engine archive: git {args[0]} failed in {engine_root!r}: {err.strip()}")
        return r.stdout
    sha = git("rev-parse", "HEAD").strip()
    return sha, git("archive", "--format=tar.gz", "HEAD", text=False)


# --- provider ---------------------------------------------------------------------


class DaytonaProvider:
    name = NAME

    # create ------------------------------------------------------------------

    def create(self, *, run_id: str, profile: Any, env: Mapping[str, str],
               engine_root: str) -> NodeEndpoint:
        opts = DaytonaOptions.resolve(profile, env)
        client = _client(env, opts)
        sdk = _sdk()
        name = sandbox_name(run_id)
        params = self._create_params(sdk, name=name, run_id=run_id, opts=opts)
        try:
            sandbox = client.create(params, timeout=opts.create_timeout_s)
        except Exception as exc:  # noqa: BLE001 — sorted below
            if not _is(exc, "DaytonaConflictError"):
                raise _unavailable("sandbox create", exc) from exc
            # A sandbox with this run's name already exists (a peer lost the
            # record, or a restart). Reuse it when its node-agent answers,
            # else replace it — never run a second sandbox for one run.
            try:
                existing = client.get(name)
            except Exception as get_exc:  # noqa: BLE001
                raise _unavailable(f"sandbox get {name}", get_exc) from get_exc
            node = self._revive(existing, ready_timeout_s=60.0)
            if node is not None:
                return node
            self._delete(existing, wait=True)
            try:
                sandbox = client.create(params, timeout=opts.create_timeout_s)
            except Exception as exc2:  # noqa: BLE001
                raise _unavailable("sandbox create (after replacing a dead one)", exc2) from exc2
        try:
            return self._boot(sandbox, opts=opts, engine_root=engine_root)
        except BaseException:
            # Nothing has recorded this sandbox yet, so nothing else would
            # delete it before its TTL.
            if not (env.get("MO_DAYTONA_KEEP_FAILED") or "").strip():
                self._delete(sandbox, wait=False)
            raise

    def _create_params(self, sdk: Any, *, name: str, run_id: str, opts: DaytonaOptions) -> Any:
        common: dict[str, Any] = {
            "name": name,
            "labels": {LABEL_SANDBOX: "1", LABEL_PROVIDER: NAME, LABEL_RUN: run_id[:63],
                       LABEL_CREATED: str(int(time.time()))},
            "env_vars": dict(opts.env),
            "ttl_minutes": opts.ttl_minutes,
            "auto_stop_interval": opts.auto_stop_minutes,
            "ephemeral": True,
        }
        if opts.domain_allow_list:
            common["domain_allow_list"] = opts.domain_allow_list
        if opts.snapshot:
            return sdk.CreateSandboxFromSnapshotParams(snapshot=opts.snapshot, **common)
        if opts.image:
            resources = None
            if opts.cpu or opts.memory_gib or opts.disk_gib:
                resources = sdk.Resources(cpu=opts.cpu, memory=opts.memory_gib,
                                          disk=opts.disk_gib)
            return sdk.CreateSandboxFromImageParams(image=opts.image, resources=resources,
                                                    **common)
        # Daytona's default snapshot: Python, git, Node, claude and opencode.
        return sdk.CreateSandboxFromSnapshotParams(**common)

    # boot ----------------------------------------------------------------------

    def _boot(self, sandbox: Any, *, opts: DaytonaOptions, engine_root: str) -> NodeEndpoint:
        home = sandbox.get_user_home_dir().rstrip("/")
        base = f"{home}/{_NODE_DIR}"
        state = f"{base}/state"
        sha, archive = _engine_archive(engine_root)
        token = secrets.token_urlsafe(32)
        meta = {"python": opts.python, "sha": sha, "state": state, "port": NODE_PORT}
        try:
            sandbox.fs.create_folder(base, "700")
            sandbox.fs.upload_file(archive, f"{base}/engine.tar.gz")
            sandbox.fs.upload_file(token.encode("utf-8"), f"{base}/node-token")
            sandbox.fs.upload_file(json.dumps(meta).encode("utf-8"), f"{base}/node.json")
            sandbox.fs.upload_file(self._boot_script(base=base, state=state, sha=sha,
                                                     opts=opts).encode("utf-8"),
                                   f"{base}/boot.sh")
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("engine upload", exc) from exc
        self._run(sandbox, f"bash {shlex.quote(base + '/boot.sh')}", timeout=600,
                  what="node bootstrap")
        if opts.setup.strip():
            try:
                sandbox.fs.upload_file(opts.setup.encode("utf-8"), f"{base}/setup.sh")
            except Exception as exc:  # noqa: BLE001
                raise _unavailable("setup upload", exc) from exc
            self._run(sandbox, f"cd {shlex.quote(home)} && bash {shlex.quote(base + '/setup.sh')}",
                      timeout=int(opts.setup_timeout_s), what="profile setup")
        # Only a fully bootstrapped sandbox may be revived (``_revive``): the
        # token and meta files exist from the first upload on, so they cannot
        # tell a finished boot from a failed one.
        try:
            sandbox.fs.upload_file(b"1", f"{base}/booted")
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("boot marker", exc) from exc
        self._start_node_agent(sandbox, base=base, meta=meta)
        node = self._endpoint(sandbox, token=token)
        if not self._wait_ready(node, opts.ready_timeout_s):
            raise ProviderUnavailableError(
                f"daytona: node-agent in sandbox {sandbox.id} not healthy after "
                f"{opts.ready_timeout_s:.0f}s; log tail:\n{self._log_tail(sandbox, base)}")
        return node

    @staticmethod
    def _boot_script(*, base: str, state: str, sha: str, opts: DaytonaOptions) -> str:
        py = shlex.quote(opts.python)
        q = shlex.quote
        deps = " ".join(f"{imp}={req}" for imp, req in _NODE_AGENT_DEPS)
        install = (f'  {py} -m pip install --user --quiet --disable-pip-version-check $missing\n'
                   if opts.bootstrap else
                   '  echo "node-agent deps missing: $missing (bootstrap disabled)" >&2; exit 3\n')
        return (
            "set -euo pipefail\n"
            "umask 077\n"
            f"mkdir -p {q(state + '/engines/' + sha)}\n"
            f"tar -xzf {q(base + '/engine.tar.gz')} -C {q(state + '/engines/' + sha)}\n"
            f"rm -f {q(base + '/engine.tar.gz')}\n"
            f"chmod 600 {q(base + '/node-token')}\n"
            "missing=\"\"\n"
            f"for pair in {deps}; do\n"
            f'  {py} -c "import ${{pair%%=*}}" 2>/dev/null || missing="$missing ${{pair#*=}}"\n'
            "done\n"
            'if [ -n "$missing" ]; then\n'
            f"{install}"
            "fi\n"
            f"PYTHONPATH={q(state + '/engines/' + sha)} {py} -c 'import mini_ork.remote.node_agent.app'\n"
        )

    def _start_node_agent(self, sandbox: Any, *, base: str, meta: Mapping[str, Any]) -> None:
        sdk = _sdk()
        q = shlex.quote
        engine = f"{meta['state']}/engines/{meta['sha']}"
        command = (
            f"cd {q(base)} && MO_NODE_TOKEN=\"$(cat {q(base + '/node-token')})\" "
            f"PYTHONPATH={q(engine)} {q(str(meta['python']))} -m mini_ork.cli.node_agent "
            f"--runtime host --state-dir {q(str(meta['state']))} --bind 127.0.0.1 "
            f"--port {int(meta['port'])} >> {q(base + '/node-agent.log')} 2>&1"
        )
        # A fresh session per start: a restart (``_revive``) never depends on
        # how the API treats a session name that already exists.
        session = f"{_SESSION_PREFIX}-{secrets.token_hex(3)}"
        try:
            sandbox.process.create_session(session)
            sandbox.process.execute_session_command(
                session, sdk.SessionExecuteRequest(command=command, run_async=True))
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("node-agent start", exc) from exc

    def _run(self, sandbox: Any, command: str, *, timeout: int, what: str) -> None:
        try:
            resp = sandbox.process.exec(command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(what, exc) from exc
        if getattr(resp, "exit_code", 1) != 0:
            out = str(getattr(resp, "result", "") or "")[-2000:]
            raise ProviderUnavailableError(
                f"daytona: {what} exited {getattr(resp, 'exit_code', '?')} in sandbox "
                f"{sandbox.id}:\n{out}")

    # endpoints -------------------------------------------------------------------

    def _endpoint(self, sandbox: Any, *, token: str) -> NodeEndpoint:
        try:
            link = sandbox.get_preview_link(NODE_PORT)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("preview link", exc) from exc
        headers = {_SKIP_WARNING_HEADER: "true"}
        if getattr(link, "token", None):
            headers[PREVIEW_TOKEN_HEADER] = str(link.token)
        # Every endpoint handed out is of a sandbox whose boot (bootstrap +
        # profile setup) finished: _boot writes the booted marker before it
        # returns one, and _revive requires that marker.
        return NodeEndpoint(name=f"daytona:{getattr(sandbox, 'name', '') or sandbox.id}",
                            url=str(link.url).rstrip("/"), token=token, headers=headers,
                            max_sessions=1, handle=str(sandbox.id), provider=NAME,
                            setup_applied=True)

    def _wait_ready(self, node: NodeEndpoint, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        delay = 0.5
        while True:
            if _http_ok(f"{node.url}/v1/health", node.headers):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)

    @staticmethod
    def _log_tail(sandbox: Any, base: str) -> str:
        try:
            data = sandbox.fs.download_file(f"{base}/node-agent.log")
        except Exception:  # noqa: BLE001
            return "(no node-agent.log)"
        return data.decode("utf-8", "replace")[-2000:]

    def _revive(self, sandbox: Any, *, ready_timeout_s: float) -> NodeEndpoint | None:
        """The endpoint of an already-booted sandbox, restarting its node-agent
        if it stopped answering. ``None`` when the sandbox never finished boot."""
        state = str(getattr(getattr(sandbox, "state", ""), "value", getattr(sandbox, "state", "")))
        if state.lower() not in ("started", ""):
            return None
        try:
            base = f"{sandbox.get_user_home_dir().rstrip('/')}/{_NODE_DIR}"
            sandbox.fs.download_file(f"{base}/booted")
            token = sandbox.fs.download_file(f"{base}/node-token").decode("utf-8").strip()
            meta = json.loads(sandbox.fs.download_file(f"{base}/node.json"))
        except Exception:  # noqa: BLE001 — never booted (or files gone)
            return None
        if not token:
            return None
        node = self._endpoint(sandbox, token=token)
        if _http_ok(f"{node.url}/v1/health", node.headers):
            return node
        self._start_node_agent(sandbox, base=base, meta=meta)
        return node if self._wait_ready(node, ready_timeout_s) else None

    # attach / release / reap --------------------------------------------------------

    def attach(self, handle: str, *, env: Mapping[str, str]) -> NodeEndpoint | None:
        client = _client(env)
        try:
            sandbox = client.get(handle)
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "DaytonaNotFoundError"):
                return None
            raise _unavailable(f"sandbox get {handle}", exc) from exc
        return self._revive(sandbox, ready_timeout_s=60.0)

    def release(self, handle: str, *, env: Mapping[str, str]) -> None:
        client = _client(env)
        try:
            sandbox = client.get(handle)
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "DaytonaNotFoundError"):
                return
            raise _unavailable(f"sandbox get {handle}", exc) from exc
        self._delete(sandbox, wait=False, strict=True)

    @staticmethod
    def _delete(sandbox: Any, *, wait: bool, strict: bool = False) -> None:
        try:
            sandbox.delete(wait=wait) if wait else sandbox.delete()
        except Exception as exc:  # noqa: BLE001
            # 404: already gone. 409 "state change in progress": already being
            # deleted (delete is fire-and-forget). Both are success.
            if _is(exc, "DaytonaNotFoundError") or _is(exc, "DaytonaConflictError"):
                return
            if strict:
                raise _unavailable(f"sandbox delete {getattr(sandbox, 'id', '?')}", exc) from exc

    def reap(self, *, max_age_s: int, dry_run: bool, env: Mapping[str, str]) -> list[str]:
        try:
            client = _client(env)
            sdk = _sdk()
            found = list(client.list(sdk.ListSandboxesQuery(
                labels={LABEL_SANDBOX: "1", LABEL_PROVIDER: NAME})))
        except Exception:  # noqa: BLE001 — fail-open: no SDK, no key, API down
            return []
        now = time.time()
        reaped: list[str] = []
        for sandbox in found:
            labels = dict(getattr(sandbox, "labels", None) or {})
            created = labels.get(LABEL_CREATED, "")
            if not created.isdigit() or now - int(created) < max_age_s:
                continue
            reaped.append(str(sandbox.id))
            if not dry_run:
                self._delete(sandbox, wait=False)
        return reaped


register_node_provider(NAME, DaytonaProvider)
