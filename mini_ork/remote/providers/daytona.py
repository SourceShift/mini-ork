"""Daytona node provider: one Daytona sandbox per run, a node-agent inside it.

``provider: daytona`` in an environment profile makes ``--placement remote``
create a Daytona sandbox for the run, boot the node-agent inside it and reach
it through Daytona's authenticated preview proxy. The boot sequence is shared
by every sandbox cloud (:mod:`mini_ork.remote.providers.sandbox_node`); this
module is only the Daytona adapter.

Daytona specifics:

* idempotency key: the sandbox **name** ``mo-<run_id>``. Names are unique per
  organization, so a lost run-dir record still finds the same sandbox.
* lifetime: a wall-clock ``ttl_minutes`` that Daytona enforces even if this
  machine dies, ``ephemeral=True`` (a stopped sandbox is deleted), and idle
  auto-stop OFF (a long, quiet agent turn must not be stopped mid-run).
* auth: the per-sandbox preview token, sent as ``x-daytona-preview-token``.
* the default snapshot ships Python, git, Node, ``claude`` and ``opencode``.

Every SDK call goes through :func:`_sdk` / :func:`_client`, so ``daytona`` is
imported only when this provider is used. A missing SDK, a missing
``DAYTONA_API_KEY`` or a refused API call is a :class:`ProviderUnavailableError`
(→ ``failure_class=remote_unavailable``), never a silent host fallback.
"""
from __future__ import annotations

import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mini_ork.remote.providers.base import (
    ProviderUnavailableError,
    register_node_provider,
    resolve_provider_secret,
)
from mini_ork.remote.providers.sandbox_node import (
    LABEL_CREATED,
    LABEL_PROVIDER,
    LABEL_RUN,
    LABEL_SANDBOX,
    BootOptions,
    SandboxNodeProvider,
    memory_gib,
    run_labels,
    sandbox_name,
    ttl_minutes_default,
)

__all__ = ["DaytonaOptions", "DaytonaProvider", "NAME", "PREVIEW_TOKEN_HEADER", "sandbox_name"]

NAME = "daytona"
PREVIEW_TOKEN_HEADER = "x-daytona-preview-token"
# Daytona serves an interstitial warning page to browsers on preview URLs; an
# API client must never get HTML where it expects JSON.
_SKIP_WARNING_HEADER = "X-Daytona-Skip-Preview-Warning"
_SESSION_PREFIX = "mo-node-agent"
# Re-exported for callers and tests that predate the shared base.
__all__ += ["LABEL_CREATED", "LABEL_PROVIDER", "LABEL_RUN", "LABEL_SANDBOX"]


def _memory_gib(raw: Any) -> int | None:
    return memory_gib(raw, provider=NAME)


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
    ttl_minutes: int = 360
    auto_stop_minutes: int = 0
    cpu: int | None = None
    memory_gib: int | None = None
    disk_gib: int | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    domain_allow_list: str = ""
    create_timeout_s: float = 300.0
    boot: BootOptions = field(default_factory=BootOptions)

    _KNOWN = frozenset({"snapshot", "target", "api_url", "ttl_minutes", "auto_stop_minutes",
                        "python", "bootstrap", "create_timeout_s", "ready_timeout_s"})

    @classmethod
    def resolve(cls, profile: Any, env: Mapping[str, str]) -> "DaytonaOptions":
        opts = dict(getattr(profile, "provider_options", None) or {})
        unknown = sorted(set(opts) - cls._KNOWN)
        if unknown:
            raise ProviderUnavailableError(
                f"daytona: unknown provider_options {unknown}; known: {sorted(cls._KNOWN)}")
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
            ttl_minutes=int(opts.get("ttl_minutes") or ttl_minutes_default(env)),
            auto_stop_minutes=int(opts.get("auto_stop_minutes") or 0),
            cpu=int(-(-float(cpus) // 1)) if cpus not in (None, "") else None,
            memory_gib=_memory_gib(resources.get("memory")),
            disk_gib=_memory_gib(disk) if disk not in (None, "") else None,
            env={str(k): str(v) for k, v in (getattr(profile, "env", None) or {}).items()},
            domain_allow_list=",".join(domains) if network == "allowlist" else "",
            create_timeout_s=float(opts.get("create_timeout_s") or 300.0),
            boot=BootOptions.from_profile(profile, opts),
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


# --- the adapter ------------------------------------------------------------------


class _DaytonaSandbox:
    """A Daytona sandbox as :class:`~.sandbox_node.SandboxHandle`."""

    def __init__(self, sandbox: Any) -> None:
        self._sb = sandbox
        self.id = str(sandbox.id)
        self.name = str(getattr(sandbox, "name", "") or sandbox.id)
        self.labels = dict(getattr(sandbox, "labels", None) or {})
        self._home = ""

    def alive(self) -> bool:
        state = getattr(self._sb, "state", "")
        state = str(getattr(state, "value", state)).lower()
        return state in ("started", "")

    def home(self) -> str:
        if not self._home:
            try:
                self._home = str(self._sb.get_user_home_dir())
            except Exception as exc:  # noqa: BLE001
                raise _unavailable("home dir", exc) from exc
        return self._home

    def upload(self, data: bytes, path: str) -> None:
        try:
            self._sb.fs.upload_file(data, path)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(f"upload {path}", exc) from exc

    def download(self, path: str) -> bytes:
        return self._sb.fs.download_file(path)

    def run(self, command: str, *, timeout: int) -> tuple[int, str]:
        try:
            resp = self._sb.process.exec(command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("exec", exc) from exc
        return int(getattr(resp, "exit_code", 1)), str(getattr(resp, "result", "") or "")

    def start_background(self, command: str) -> None:
        # A fresh session per start: a restart never depends on how the API
        # treats a session name that already exists.
        session = f"{_SESSION_PREFIX}-{secrets.token_hex(3)}"
        try:
            self._sb.process.create_session(session)
            self._sb.process.execute_session_command(
                session, _sdk().SessionExecuteRequest(command=command, run_async=True))
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("node-agent start", exc) from exc

    def endpoint(self, port: int) -> tuple[str, dict[str, str]]:
        try:
            link = self._sb.get_preview_link(port)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("preview link", exc) from exc
        headers = {_SKIP_WARNING_HEADER: "true"}
        if getattr(link, "token", None):
            headers[PREVIEW_TOKEN_HEADER] = str(link.token)
        return str(link.url), headers

    def delete(self, *, wait: bool) -> None:
        try:
            self._sb.delete(wait=wait) if wait else self._sb.delete()
        except Exception as exc:  # noqa: BLE001
            # 404: already gone. 409 "state change in progress": already being
            # deleted (delete is fire-and-forget). Both are success.
            if _is(exc, "DaytonaNotFoundError") or _is(exc, "DaytonaConflictError"):
                return
            raise _unavailable(f"sandbox delete {self.id}", exc) from exc


class DaytonaProvider(SandboxNodeProvider):
    name = NAME

    def boot_options(self, profile: Any, env: Mapping[str, str]) -> BootOptions:
        return DaytonaOptions.resolve(profile, env).boot

    def spawn_sandbox(self, *, run_id: str, profile: Any,
                      env: Mapping[str, str]) -> _DaytonaSandbox:
        opts = DaytonaOptions.resolve(profile, env)
        client = _client(env, opts)
        params = self._create_params(_sdk(), name=sandbox_name(run_id), run_id=run_id, opts=opts)
        try:
            return _DaytonaSandbox(client.create(params, timeout=opts.create_timeout_s))
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("sandbox create", exc) from exc

    def find_run_sandbox(self, *, run_id: str, env: Mapping[str, str]) -> _DaytonaSandbox | None:
        return self.get_sandbox(sandbox_name(run_id), env)

    def get_sandbox(self, handle: str, env: Mapping[str, str]) -> _DaytonaSandbox | None:
        try:
            return _DaytonaSandbox(_client(env).get(handle))
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "DaytonaNotFoundError"):
                return None
            raise _unavailable(f"sandbox get {handle}", exc) from exc

    def list_sandboxes(self, env: Mapping[str, str]) -> Iterable[_DaytonaSandbox]:
        query = _sdk().ListSandboxesQuery(labels={LABEL_SANDBOX: "1", LABEL_PROVIDER: NAME})
        return [_DaytonaSandbox(sb) for sb in _client(env).list(query)]

    @staticmethod
    def _create_params(sdk: Any, *, name: str, run_id: str, opts: DaytonaOptions) -> Any:
        common: dict[str, Any] = {
            "name": name,
            "labels": run_labels(NAME, run_id),
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


register_node_provider(NAME, DaytonaProvider)
