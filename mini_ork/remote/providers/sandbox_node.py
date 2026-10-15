"""Shared machinery for node providers whose node is a cloud sandbox.

Every sandbox cloud (Daytona, E2B, Modal, Vercel, …) needs the same thing from
mini-ork: create one sandbox per run, boot ``mini-ork node-agent --runtime host``
inside it from the control plane's own engine, reach it through the cloud's
authenticated port proxy, and destroy it afterwards. That algorithm lives here,
once. A cloud contributes only a :class:`SandboxHandle` adapter (upload, run,
start a background process, an endpoint URL + auth headers, delete) and four
hooks on :class:`SandboxNodeProvider`.

The boot sequence, proven live on Daytona (``docs/architecture/
cloud-sandbox-providers.md``):

1. find this run's sandbox (a cloud-side idempotency key: a unique name, or
   ``mo.run`` metadata). Revive it when it finished booting and its node-agent
   answers, else replace it. Never two sandboxes for one run.
2. create the sandbox, labelled ``mo.sandbox`` / ``mo.provider`` / ``mo.run`` /
   ``mo.created`` for the TTL sweep.
3. upload ``git archive HEAD`` of the engine into the node-agent's engine store
   (``<state>/engines/<sha>``), so the remote workspace finds that sha in
   ``/v1/health`` and skips its own upload.
4. install whichever node-agent deps the image lacks into a venv that also
   sees the system site-packages, never ``pip --user`` (a user-site package
   shadows the system for the code under test, too).
5. run the profile's ``setup`` once, then write the ``booted`` marker. Only a
   booted sandbox is ever revived.
6. start the node-agent on ``127.0.0.1:7091`` with a per-sandbox bearer token
   that lives in a 0600 file in the sandbox and in the node-agent's own env
   only (spawned agents never inherit it), then wait for ``/v1/health``.

Endpoints carry ``setup_applied=True``: the provider ran the profile's setup,
so the remote workspace must not ask the host-runtime node-agent for docker
image preparation.
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from mini_ork.remote.providers.base import NodeEndpoint, ProviderUnavailableError

__all__ = [
    "BootOptions",
    "DEFAULT_TTL_MIN",
    "LABEL_CREATED",
    "LABEL_PROVIDER",
    "LABEL_RUN",
    "LABEL_SANDBOX",
    "NODE_DIR",
    "NODE_PORT",
    "SandboxHandle",
    "SandboxNodeProvider",
    "boot_script",
    "engine_archive",
    "http_ok",
    "memory_gib",
    "run_labels",
    "sandbox_name",
    "start_command",
    "ttl_minutes_default",
]

NODE_PORT = 7091
NODE_DIR = ".mini-ork-node"          # under the sandbox user's home
LABEL_SANDBOX = "mo.sandbox"
LABEL_PROVIDER = "mo.provider"
LABEL_RUN = "mo.run"
LABEL_CREATED = "mo.created"
DEFAULT_TTL_MIN = 360                # = sandbox_reaper.DEFAULT_MAX_AGE_S
_REVIVE_READY_S = 60.0
# import name → pip requirement; what the node-agent itself needs to start.
NODE_AGENT_DEPS = (("fastapi", "fastapi"), ("uvicorn", "uvicorn[standard]"),
                   ("yaml", "pyyaml"), ("jsonschema", "jsonschema"))


# --- small shared helpers ------------------------------------------------------


def sandbox_name(run_id: str) -> str:
    """A DNS-safe, bounded name for a run's sandbox (a cloud idempotency key)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    return f"mo-{slug}"[:63].rstrip("-")


def memory_gib(raw: Any, *, provider: str = "sandbox") -> int | None:
    """``4``, ``"4g"``, ``"4096m"`` → GiB (rounded up, minimum 1)."""
    if raw in (None, ""):
        return None
    text = str(raw).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([gm]i?b?)?", text)
    if not m:
        raise ProviderUnavailableError(f"{provider}: cannot read memory size {raw!r} "
                                       "(use e.g. 4, '4g' or '4096m')")
    value = float(m.group(1))
    if (m.group(2) or "g").startswith("m"):
        value /= 1024
    return max(1, int(-(-value // 1)))


def ttl_minutes_default(env: Mapping[str, str]) -> int:
    """The sandbox lifetime cap: ``MO_SANDBOX_MAX_AGE`` (seconds) / 60, else 6 h."""
    max_age = (env.get("MO_SANDBOX_MAX_AGE") or "").strip()
    return max(1, int(max_age) // 60) if max_age.isdigit() else DEFAULT_TTL_MIN


def run_labels(provider: str, run_id: str) -> dict[str, str]:
    """The labels/metadata every mini-ork sandbox carries (GC + idempotency)."""
    return {LABEL_SANDBOX: "1", LABEL_PROVIDER: provider, LABEL_RUN: run_id[:63],
            LABEL_CREATED: str(int(time.time()))}


def http_ok(url: str, headers: Mapping[str, str], timeout: float = 10.0) -> bool:
    """One GET; True on 2xx. The network seam besides the cloud SDKs (tests fake it)."""
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def engine_archive(engine_root: str) -> tuple[str, bytes]:
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
                f"engine archive: git {args[0]} failed in {engine_root!r}: {err.strip()}")
        return r.stdout
    sha = git("rev-parse", "HEAD").strip()
    return sha, git("archive", "--format=tar.gz", "HEAD", text=False)


@dataclass(frozen=True)
class BootOptions:
    """How the node-agent is booted inside any sandbox."""

    python: str = "python3"
    bootstrap: bool = True
    setup: str = ""
    setup_timeout_s: float = 900.0
    ready_timeout_s: float = 180.0

    @classmethod
    def from_profile(cls, profile: Any, provider_options: Mapping[str, Any]) -> "BootOptions":
        return cls(
            python=str(provider_options.get("python") or "python3"),
            bootstrap=bool(provider_options.get("bootstrap", True)),
            setup=str(getattr(profile, "setup", "") or ""),
            setup_timeout_s=float(getattr(profile, "setup_timeout_s", 900.0) or 900.0),
            ready_timeout_s=float(provider_options.get("ready_timeout_s") or 180.0),
        )


def boot_script(*, base: str, state: str, sha: str, opts: BootOptions) -> str:
    """The one-shot bootstrap. Missing node-agent deps go into a dedicated venv
    that also sees the system site-packages, never ``pip --user``: a user-site
    install shadows the system for the code under test too (``fastapi --user``
    pulled an ``opentelemetry-api`` that broke every pytest run in a Daytona
    sandbox, run-1791913440-97590). The interpreter the node-agent must use is
    recorded in ``node-python`` for start and revive."""
    py = shlex.quote(opts.python)
    q = shlex.quote
    venv = base + "/venv"
    deps = " ".join(f"{imp}={req}" for imp, req in NODE_AGENT_DEPS)
    install = (
        f"  {py} -m venv --system-site-packages {q(venv)}\n"
        f"  {q(venv + '/bin/python')} -m pip install --quiet --disable-pip-version-check $missing\n"
        f"  node_py={q(venv + '/bin/python')}\n"
        if opts.bootstrap else
        '  echo "node-agent deps missing: $missing (bootstrap disabled)" >&2; exit 3\n')
    return (
        "set -euo pipefail\n"
        "umask 077\n"
        f"mkdir -p {q(state + '/engines/' + sha)}\n"
        f"tar -xzf {q(base + '/engine.tar.gz')} -C {q(state + '/engines/' + sha)}\n"
        f"rm -f {q(base + '/engine.tar.gz')}\n"
        f"chmod 600 {q(base + '/node-token')}\n"
        f"node_py={py}\n"
        "missing=\"\"\n"
        f"for pair in {deps}; do\n"
        f'  {py} -c "import ${{pair%%=*}}" 2>/dev/null || missing="$missing ${{pair#*=}}"\n'
        "done\n"
        'if [ -n "$missing" ]; then\n'
        f"{install}"
        "fi\n"
        f'printf "%s\\n" "$node_py" > {q(base + "/node-python")}\n'
        f"PYTHONPATH={q(state + '/engines/' + sha)} \"$node_py\" -c 'import mini_ork.remote.node_agent.app'\n"
    )


def start_command(*, base: str, meta: Mapping[str, Any]) -> str:
    """The node-agent start line: token only in its own env, the recorded python."""
    q = shlex.quote
    engine = f"{meta['state']}/engines/{meta['sha']}"
    return (
        f"cd {q(base)} && MO_NODE_TOKEN=\"$(cat {q(base + '/node-token')})\" "
        f"PYTHONPATH={q(engine)} \"$(cat {q(base + '/node-python')} 2>/dev/null "
        f"|| echo {q(str(meta['python']))})\" -m mini_ork.cli.node_agent "
        f"--runtime host --state-dir {q(str(meta['state']))} --bind 127.0.0.1 "
        f"--port {int(meta['port'])} >> {q(base + '/node-agent.log')} 2>&1"
    )


# --- the adapter a cloud implements ------------------------------------------------


@runtime_checkable
class SandboxHandle(Protocol):
    """One live sandbox, as the boot sequence needs it.

    ``id`` is the provider's handle (recorded in the run dir); ``labels`` is the
    metadata it was created with (the TTL sweep reads ``mo.created``). Every
    method raises :class:`ProviderUnavailableError` on a cloud failure, except
    ``download`` (any exception means "not there") and ``delete`` (a sandbox
    that is already gone, or already being deleted, is success)."""

    id: str
    name: str
    labels: Mapping[str, str]

    def alive(self) -> bool: ...

    def home(self) -> str: ...

    def upload(self, data: bytes, path: str) -> None: ...

    def download(self, path: str) -> bytes: ...

    def run(self, command: str, *, timeout: int) -> tuple[int, str]: ...

    def start_background(self, command: str) -> None: ...

    def endpoint(self, port: int) -> tuple[str, dict[str, str]]: ...

    def delete(self, *, wait: bool) -> None: ...


class SandboxNodeProvider:
    """A :class:`~mini_ork.remote.providers.base.NodeProvider` over any sandbox cloud.

    Subclasses set ``name`` and implement the four hooks; the four NodeProvider
    verbs (``create`` / ``attach`` / ``release`` / ``reap``) are shared."""

    name = ""
    # What the UI shows about this provider without touching the network: a
    # one-line description, the SDK module that must be importable, and the
    # credential NAMES it reads through the vault (never values).
    kind = "cloud sandbox per run"
    sdk_module = ""
    credential_keys: tuple[str, ...] = ()

    # --- hooks ---------------------------------------------------------------

    def boot_options(self, profile: Any, env: Mapping[str, str]) -> BootOptions:
        """Validate the profile's provider options (fail before any API call)."""
        raise NotImplementedError

    def spawn_sandbox(self, *, run_id: str, profile: Any,
                      env: Mapping[str, str]) -> SandboxHandle:
        """Create the run's sandbox, labelled with :func:`run_labels`."""
        raise NotImplementedError

    def find_run_sandbox(self, *, run_id: str, env: Mapping[str, str]) -> SandboxHandle | None:
        """The run's existing sandbox, if the cloud has one (idempotency)."""
        raise NotImplementedError

    def get_sandbox(self, handle: str, env: Mapping[str, str]) -> SandboxHandle | None:
        """The sandbox with this handle, ``None`` when it no longer exists."""
        raise NotImplementedError

    def list_sandboxes(self, env: Mapping[str, str]) -> Iterable[SandboxHandle]:
        """Every mini-ork sandbox of this provider (for the TTL sweep)."""
        raise NotImplementedError

    # --- NodeProvider verbs ---------------------------------------------------

    def create(self, *, run_id: str, profile: Any, env: Mapping[str, str],
               engine_root: str) -> NodeEndpoint:
        boot = self.boot_options(profile, env)
        existing = self.find_run_sandbox(run_id=run_id, env=env)
        if existing is not None:
            # A peer lost the run-dir record, or a restart: reuse a booted,
            # answering sandbox, else replace it. Never two for one run.
            node = self._revive(existing, ready_timeout_s=_REVIVE_READY_S)
            if node is not None:
                return node
            self._quiet_delete(existing, wait=True)
        sandbox = self.spawn_sandbox(run_id=run_id, profile=profile, env=env)
        try:
            return self._boot(sandbox, boot=boot, engine_root=engine_root)
        except BaseException:
            # Nothing has recorded this sandbox yet, so nothing else would
            # delete it before its TTL.
            if not (env.get("MO_SANDBOX_KEEP_FAILED") or env.get("MO_DAYTONA_KEEP_FAILED")
                    or "").strip():
                self._quiet_delete(sandbox, wait=False)
            raise

    def attach(self, handle: str, *, env: Mapping[str, str]) -> NodeEndpoint | None:
        sandbox = self.get_sandbox(handle, env)
        return None if sandbox is None else self._revive(sandbox, ready_timeout_s=_REVIVE_READY_S)

    def release(self, handle: str, *, env: Mapping[str, str]) -> None:
        sandbox = self.get_sandbox(handle, env)
        if sandbox is not None:
            sandbox.delete(wait=False)

    def reap(self, *, max_age_s: int, dry_run: bool, env: Mapping[str, str]) -> list[str]:
        try:
            found = list(self.list_sandboxes(env))
        except Exception:  # noqa: BLE001 — fail-open: no SDK, no key, API down
            return []
        now = time.time()
        reaped: list[str] = []
        for sandbox in found:
            created = str((sandbox.labels or {}).get(LABEL_CREATED, ""))
            if not created.isdigit() or now - int(created) < max_age_s:
                continue
            reaped.append(sandbox.id)
            if not dry_run:
                self._quiet_delete(sandbox, wait=False)
        return reaped

    # --- boot -----------------------------------------------------------------

    def _boot(self, sandbox: SandboxHandle, *, boot: BootOptions,
              engine_root: str) -> NodeEndpoint:
        home = sandbox.home().rstrip("/")
        base = f"{home}/{NODE_DIR}"
        state = f"{base}/state"
        sha, archive = engine_archive(engine_root)
        token = secrets.token_urlsafe(32)
        meta = {"python": boot.python, "sha": sha, "state": state, "port": NODE_PORT}
        sandbox.run(f"mkdir -p {shlex.quote(base)} && chmod 700 {shlex.quote(base)}",
                    timeout=60)
        sandbox.upload(archive, f"{base}/engine.tar.gz")
        sandbox.upload(token.encode("utf-8"), f"{base}/node-token")
        sandbox.upload(json.dumps(meta).encode("utf-8"), f"{base}/node.json")
        sandbox.upload(boot_script(base=base, state=state, sha=sha, opts=boot).encode("utf-8"),
                       f"{base}/boot.sh")
        self._run(sandbox, f"bash {shlex.quote(base + '/boot.sh')}", timeout=600,
                  what="node bootstrap")
        if boot.setup.strip():
            sandbox.upload(boot.setup.encode("utf-8"), f"{base}/setup.sh")
            self._run(sandbox, f"cd {shlex.quote(home)} && bash {shlex.quote(base + '/setup.sh')}",
                      timeout=int(boot.setup_timeout_s), what="profile setup")
        # Only a fully bootstrapped sandbox may be revived: the token and meta
        # files exist from the first upload on, so they cannot tell a finished
        # boot from a failed one.
        sandbox.upload(b"1", f"{base}/booted")
        sandbox.start_background(start_command(base=base, meta=meta))
        node = self._endpoint(sandbox, token=token)
        if not self._wait_ready(node, boot.ready_timeout_s):
            raise ProviderUnavailableError(
                f"{self.name}: node-agent in sandbox {sandbox.id} not healthy after "
                f"{boot.ready_timeout_s:.0f}s; log tail:\n{self._log_tail(sandbox, base)}")
        return node

    def _run(self, sandbox: SandboxHandle, command: str, *, timeout: int, what: str) -> None:
        rc, out = sandbox.run(command, timeout=timeout)
        if rc != 0:
            raise ProviderUnavailableError(
                f"{self.name}: {what} exited {rc} in sandbox {sandbox.id}:\n{out[-2000:]}")

    def _endpoint(self, sandbox: SandboxHandle, *, token: str) -> NodeEndpoint:
        url, headers = sandbox.endpoint(NODE_PORT)
        return NodeEndpoint(name=f"{self.name}:{sandbox.name or sandbox.id}",
                            url=str(url).rstrip("/"), token=token, headers=dict(headers),
                            max_sessions=1, handle=str(sandbox.id), provider=self.name,
                            setup_applied=True)

    @staticmethod
    def _wait_ready(node: NodeEndpoint, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        delay = 0.5
        while True:
            if http_ok(f"{node.url}/v1/health", node.headers):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)

    @staticmethod
    def _log_tail(sandbox: SandboxHandle, base: str) -> str:
        try:
            return sandbox.download(f"{base}/node-agent.log").decode("utf-8", "replace")[-2000:]
        except Exception:  # noqa: BLE001
            return "(no node-agent.log)"

    def _revive(self, sandbox: SandboxHandle, *, ready_timeout_s: float) -> NodeEndpoint | None:
        """The endpoint of an already-booted sandbox, restarting its node-agent
        if it stopped answering. ``None`` when the sandbox never finished boot."""
        try:
            if not sandbox.alive():
                return None
            base = f"{sandbox.home().rstrip('/')}/{NODE_DIR}"
            sandbox.download(f"{base}/booted")
            token = sandbox.download(f"{base}/node-token").decode("utf-8").strip()
            meta = json.loads(sandbox.download(f"{base}/node.json"))
        except Exception:  # noqa: BLE001 — never booted (or files gone)
            return None
        if not token:
            return None
        node = self._endpoint(sandbox, token=token)
        if http_ok(f"{node.url}/v1/health", node.headers):
            return node
        sandbox.start_background(start_command(base=base, meta=meta))
        return node if self._wait_ready(node, ready_timeout_s) else None

    @staticmethod
    def _quiet_delete(sandbox: SandboxHandle, *, wait: bool) -> None:
        try:
            sandbox.delete(wait=wait)
        except Exception:  # noqa: BLE001 — cleanup; the TTL is the net
            pass
