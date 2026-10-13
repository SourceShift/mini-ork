"""Node providers — how a node-agent comes into existence (cloud sandbox axis).

mini-ork's remote placement speaks ONE wire protocol: the node-agent v1 HTTP API
(``docs/architecture/remote-nodes.md``). Everything hard about remote execution
— git-snapshot tree sync, the run-dir mirror, detached procs that survive a
dropped connection, per-lane secret scoping, engine pinning — lives behind that
protocol and is shared by every node.

What differs between clouds is only how a node is *obtained*, the same split
Kubernetes draws between the kubelet (one protocol on every node) and the node
pool / cluster autoscaler (where nodes come from). A :class:`NodeProvider` is
that second half:

    static   — an operator-run node-agent on a VM you control
               (``config/nodes.yaml`` / ``MO_NODE_URL``). Nothing is created or
               destroyed; ``create`` just selects the configured node.
    daytona  — a Daytona sandbox per run, with the node-agent booted inside it
               and reached through Daytona's authenticated preview proxy.
    …        — E2B, Modal, a Kubernetes pod: one module each, same contract.

Adding a provider is one module that calls :func:`register_node_provider` at
import, plus (for a built-in) one line in :data:`_LAZY_PROVIDERS`. No resolver,
session, reaper or kill path needs to learn its name.

**One node per run, across processes.** Every dispatch and check of a run is its
own OS process with its own workspace object, and all of them must converge on
the SAME node. :func:`acquire_run_node` therefore serializes provisioning with a
file lock in the run dir and records the provider's handle in
``<run_dir>/.node-provider.json``; a later process attaches to the recorded node
instead of creating another one. The record holds names and ids only — never a
token or a header value.

Import-time contract: stdlib only, no I/O, no env read. A provider's SDK is
imported inside its own methods, so selecting ``static`` never pulls a cloud SDK.
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_PROVIDER",
    "ENV_PROVIDER",
    "NodeEndpoint",
    "NodeProvider",
    "NodeProviderFactory",
    "ProviderUnavailableError",
    "acquire_run_node",
    "attach_run_node",
    "get_node_provider",
    "known_node_providers",
    "node_record_path",
    "provider_name_for",
    "read_run_node_record",
    "reap_node_providers",
    "register_lazy_node_provider",
    "register_node_provider",
    "release_run_node",
    "resolve_provider_secret",
]

ENV_PROVIDER = "MO_NODE_PROVIDER"
DEFAULT_PROVIDER = "static"
_RECORD_NAME = ".node-provider.json"
_LOCK_NAME = ".node-provider.lock"
_ENV_PROVISION_WAIT = "MO_NODE_PROVISION_WAIT_S"
_DEFAULT_PROVISION_WAIT_S = 900.0


class ProviderUnavailableError(RuntimeError):
    """A provider cannot produce a node: SDK missing, credential missing,
    quota hit, or the cloud API refused. Always loud — remote placement never
    falls back to the host. The remote workspace maps it to
    ``failure_class=remote_unavailable``."""


@dataclass(frozen=True)
class NodeEndpoint:
    """How the control plane reaches one node-agent.

    ``token`` (the node-agent bearer) and ``headers`` (provider-level auth such
    as Daytona's ``x-daytona-preview-token``) are secrets: they are kept out of
    ``repr`` and out of :meth:`locator`, which is the only shape that may be
    written to disk. ``handle`` is the provider's own id for the node (a sandbox
    id); it is empty for a static node, which nothing created and nothing may
    destroy.
    """

    name: str
    url: str
    token: str = field(default="", repr=False)
    token_env: str = "MO_NODE_TOKEN"
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    max_sessions: int = 1
    handle: str = ""
    provider: str = DEFAULT_PROVIDER
    # True when the provider itself ran the profile's ``setup`` on the node (a
    # sandbox booted with it). The remote workspace then skips the node-agent's
    # docker image preparation, which a host-runtime node cannot do anyway.
    setup_applied: bool = False

    def locator(self) -> dict[str, Any]:
        """The non-secret description of where this node lives (for markers)."""
        return {"provider": self.provider, "handle": self.handle,
                "name": self.name, "url": self.url, "token_env": self.token_env}


@runtime_checkable
class NodeProvider(Protocol):
    """Obtains, re-finds and destroys node-agents. Implementations must be
    safe to construct with no credentials (a missing key fails at ``create``/
    ``attach``, not at import or construction) and must never raise from
    ``release``/``reap`` for a node that is already gone."""

    name: str

    def create(self, *, run_id: str, profile: Any, env: Mapping[str, str],
               engine_root: str) -> NodeEndpoint:
        """Bring up a reachable node-agent for ``run_id`` and return it.

        ``profile`` is the run's environment profile
        (:class:`mini_ork.remote.environments.Environment`) or ``None``.
        ``engine_root`` is the control plane's mini-ork checkout, for providers
        that must boot a node-agent from the same engine. Should be idempotent
        on ``run_id`` where the cloud allows it (a deterministic name), as a
        second line of defense behind :func:`acquire_run_node`'s lock."""
        ...

    def attach(self, handle: str, *, env: Mapping[str, str]) -> NodeEndpoint | None:
        """Re-resolve an existing node by ``handle`` without creating anything.
        ``None`` when the node no longer exists (deleted, TTL-expired)."""
        ...

    def release(self, handle: str, *, env: Mapping[str, str]) -> None:
        """Destroy a node this provider created. Idempotent; a missing node is
        success. A static provider does nothing."""
        ...

    def reap(self, *, max_age_s: int, dry_run: bool,
             env: Mapping[str, str]) -> list[str]:
        """TTL sweep: destroy this provider's mini-ork-labelled nodes older than
        ``max_age_s`` and return their handles. Fail-open: no credential or no
        SDK returns ``[]``."""
        ...


NodeProviderFactory = Callable[[], NodeProvider]

_PROVIDERS: dict[str, NodeProviderFactory] = {}
# Built-in providers, imported only when first selected. A module here must
# call ``register_node_provider(<name>, …)`` at import.
_LAZY_PROVIDERS: dict[str, str] = {
    "static": "mini_ork.remote.providers.static",
    "daytona": "mini_ork.remote.providers.daytona",
    "e2b": "mini_ork.remote.providers.e2b",
    "modal": "mini_ork.remote.providers.modal_sandbox",
}


def register_node_provider(name: str, factory: NodeProviderFactory) -> None:
    """Register ``factory`` under ``name`` (last write wins, like the other
    mini-ork registries)."""
    _PROVIDERS[name] = factory


def register_lazy_node_provider(name: str, module: str) -> None:
    """Declare that importing ``module`` registers provider ``name`` — for a
    plugin that wants to be selectable without being imported up front."""
    _LAZY_PROVIDERS[name] = module


def known_node_providers() -> list[str]:
    """Every selectable provider name (registered or lazily importable)."""
    return sorted(set(_PROVIDERS) | set(_LAZY_PROVIDERS))


def get_node_provider(name: str) -> NodeProvider:
    """Resolve ``name`` to a provider instance; unknown → ``ValueError``."""
    if name not in _PROVIDERS and name in _LAZY_PROVIDERS:
        try:
            importlib.import_module(_LAZY_PROVIDERS[name])
        except ImportError as exc:
            raise ProviderUnavailableError(
                f"node provider {name!r} could not be loaded from "
                f"{_LAZY_PROVIDERS[name]}: {exc}") from exc
    factory = _PROVIDERS.get(name)
    if factory is None:
        raise ValueError(f"unknown node provider {name!r}; known: "
                         f"{', '.join(known_node_providers())}")
    return factory()


def provider_name_for(profile: Any, env: Mapping[str, str]) -> str:
    """The provider a run uses: the profile's ``provider:`` field, else
    ``MO_NODE_PROVIDER``, else ``static`` (today's behavior)."""
    from_profile = str(getattr(profile, "provider", "") or "").strip()
    return from_profile or (env.get(ENV_PROVIDER) or "").strip() or DEFAULT_PROVIDER


def resolve_provider_secret(key: str, env: Mapping[str, str], *, provider: str) -> str:
    """Read a provider credential through the secret vault chain.

    The chain is environment → ``$MINI_ORK_HOME/config/secrets.local.sh`` by
    default (the zero-setup tier), or whatever ``config/vault.yaml`` declares —
    sops, a ``command`` backend such as ``pass``/``op``/``infisical``, or
    OpenBao (the secure tier). Switching tiers changes that one file, never a
    call site. Fails closed: a missing value is a :class:`ProviderUnavailableError`
    naming the key, never an empty string handed to the cloud API."""
    value = (env.get(key) or "").strip()
    if not value:
        try:
            from mini_ork.dispatch.vault import vault_environment

            value = (vault_environment(env).resolve(key) or "").strip()
        except Exception as exc:  # noqa: BLE001 — a broken vault config is a setup error
            raise ProviderUnavailableError(
                f"node provider {provider!r}: secret vault unusable while "
                f"resolving {key}: {exc}") from exc
    if not value:
        raise ProviderUnavailableError(
            f"node provider {provider!r} needs {key}: set it in the environment, "
            f"in $MINI_ORK_HOME/config/secrets.local.sh, or in a backend declared "
            f"in config/vault.yaml (check with: mini-ork vault which {key})")
    return value


# --- one node per run --------------------------------------------------------


def node_record_path(run_dir: str | os.PathLike[str]) -> Path:
    return Path(run_dir) / _RECORD_NAME


def read_run_node_record(run_dir: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The run's recorded node (``{provider, handle, run_id, created_at}``),
    or ``None`` when absent or unreadable."""
    try:
        data = json.loads(node_record_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("provider") else None


def _write_record(run_dir: Path, payload: dict[str, Any]) -> None:
    path = node_record_path(run_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class _RunLock:
    """An exclusive ``flock`` on ``<run_dir>/.node-provider.lock``, waited for
    up to ``MO_NODE_PROVISION_WAIT_S`` (a peer may be mid-boot)."""

    def __init__(self, run_dir: Path, env: Mapping[str, str]) -> None:
        self._path = run_dir / _LOCK_NAME
        raw = (env.get(_ENV_PROVISION_WAIT) or "").strip()
        try:
            self._wait_s = float(raw) if raw else _DEFAULT_PROVISION_WAIT_S
        except ValueError:
            self._wait_s = _DEFAULT_PROVISION_WAIT_S
        self._fd: int | None = None

    def __enter__(self) -> "_RunLock":
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self._wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise ProviderUnavailableError(
                        f"timed out after {self._wait_s:.0f}s waiting for another "
                        f"process to provision this run's node ({self._path})")
                time.sleep(0.2)
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        import fcntl

        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def acquire_run_node(provider: NodeProvider, *, run_id: str,
                     run_dir: str | os.PathLike[str], profile: Any,
                     env: Mapping[str, str], engine_root: str) -> NodeEndpoint:
    """The run's node: attach to the recorded one, or create it exactly once.

    Under the run-dir lock: a record naming this provider whose node still
    exists is attached; a record whose node is gone (TTL-expired, deleted) is
    replaced by a fresh node; no record means create. Static nodes (empty
    handle) are never recorded — they are simply re-selected each time."""
    if not run_id:
        raise ValueError("acquire_run_node requires a run_id")
    if not str(run_dir or "").strip():
        raise ValueError(
            f"node provider {provider.name!r} needs the run dir to share one node "
            "across the run's processes (MINI_ORK_RUN_DIR is unset)")
    rd = Path(run_dir)
    with _RunLock(rd, env):
        record = read_run_node_record(rd)
        if record and record.get("provider") == provider.name and record.get("handle"):
            node = provider.attach(str(record["handle"]), env=env)
            if node is not None:
                return node
        node = provider.create(run_id=run_id, profile=profile, env=env,
                               engine_root=engine_root)
        if node.handle:
            _write_record(rd, {
                "provider": provider.name, "handle": node.handle, "run_id": run_id,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **({"replaced": record.get("handle")} if record else {}),
            })
        return node


def attach_run_node(run_dir: str | os.PathLike[str],
                    env: Mapping[str, str]) -> NodeEndpoint | None:
    """Re-find the run's recorded node without creating one — the teardown
    path (``kill_run``, run-end release) uses this, never :func:`acquire_run_node`."""
    record = read_run_node_record(run_dir) if str(run_dir or "").strip() else None
    if not record or not record.get("handle"):
        return None
    return get_node_provider(str(record["provider"])).attach(str(record["handle"]), env=env)


def release_run_node(run_dir: str | os.PathLike[str], env: Mapping[str, str]) -> bool:
    """Destroy the run's recorded node and drop the record. Best-effort and
    idempotent: returns ``True`` when a record existed and ``release`` ran
    without raising."""
    record = read_run_node_record(run_dir) if str(run_dir or "").strip() else None
    if not record or not record.get("handle"):
        return False
    try:
        get_node_provider(str(record["provider"])).release(str(record["handle"]), env=env)
    except Exception:  # noqa: BLE001 — teardown never raises; the TTL reaper is the net
        return False
    try:
        node_record_path(run_dir).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def reap_node_providers(*, max_age_s: int, dry_run: bool, env: Mapping[str, str],
                        names: list[str] | None = None) -> dict[str, list[str]]:
    """Run each provider's TTL sweep; ``{name: [handles]}``. A provider that
    cannot load or whose sweep raises reports ``[]`` (fail-open)."""
    out: dict[str, list[str]] = {}
    for name in names or known_node_providers():
        try:
            out[name] = list(get_node_provider(name).reap(
                max_age_s=max_age_s, dry_run=dry_run, env=env))
        except Exception:  # noqa: BLE001
            out[name] = []
    return out
