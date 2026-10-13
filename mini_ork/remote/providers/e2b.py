"""E2B node provider: one E2B sandbox (a Firecracker microVM) per run.

``provider: e2b`` in an environment profile makes ``--placement remote`` create
an E2B sandbox for the run, boot the node-agent inside it, and reach it through
E2B's port proxy with public traffic **off**: every request must carry the
sandbox's ``e2b-traffic-access-token``. The boot sequence is shared by every
sandbox cloud (:mod:`mini_ork.remote.providers.sandbox_node`); this module is
only the E2B adapter.

E2B specifics (probed live 2026-10-13, SDK 2.56):

* the ``base`` template has Python 3.11, git and Node 20, but no ``claude`` /
  ``opencode`` / ``pytest`` and only ~0.5 GiB RAM. Put the agent CLIs and test
  tooling in the profile's ``setup:``, or better, build a template with them and
  more memory (``e2b template build``) and name it in ``provider_options.template``.
  CPU and memory come from the template, not the profile.
* lifetime: the sandbox ``timeout``. E2B caps it at **1 h on Hobby** and 24 h on
  Pro, so the default is 1 h. Raise ``provider_options.timeout_s`` on Pro.
* idempotency: sandboxes carry the ``mo.*`` labels as metadata; this run's
  sandbox is found by ``mo.run``.
* auth: the traffic access token, which ``Sandbox.connect`` restores, so
  ``attach`` from any process rebuilds the endpoint.

``E2B_API_KEY`` is read through the vault chain and fails closed.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mini_ork.remote.providers.base import (
    ProviderUnavailableError,
    register_node_provider,
    resolve_provider_secret,
)
from mini_ork.remote.providers.sandbox_node import (
    LABEL_PROVIDER,
    LABEL_RUN,
    LABEL_SANDBOX,
    BootOptions,
    SandboxNodeProvider,
    run_labels,
    ttl_minutes_default,
)

__all__ = ["E2BOptions", "E2BProvider", "NAME", "TRAFFIC_TOKEN_HEADER"]

NAME = "e2b"
TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token"
_HOBBY_MAX_TIMEOUT_S = 3600


@dataclass(frozen=True)
class E2BOptions:
    """``provider_options`` keys (all optional): ``template``, ``timeout_s``,
    ``domain``, ``python``, ``bootstrap``, ``ready_timeout_s``."""

    template: str = ""
    timeout_s: int = _HOBBY_MAX_TIMEOUT_S
    domain: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    boot: BootOptions = field(default_factory=BootOptions)

    _KNOWN = frozenset({"template", "timeout_s", "domain", "python", "bootstrap",
                        "ready_timeout_s"})

    @classmethod
    def resolve(cls, profile: Any, env: Mapping[str, str]) -> "E2BOptions":
        opts = dict(getattr(profile, "provider_options", None) or {})
        unknown = sorted(set(opts) - cls._KNOWN)
        if unknown:
            raise ProviderUnavailableError(
                f"e2b: unknown provider_options {unknown}; known: {sorted(cls._KNOWN)}")
        if str(getattr(profile, "network", "") or "full") == "allowlist":
            # Fail closed rather than run with full egress the profile forbade.
            raise ProviderUnavailableError(
                "e2b: network: allowlist is not supported by this provider yet; "
                "use network: full, or the daytona provider (domain_allow_list)")
        if getattr(profile, "resources", None):
            raise ProviderUnavailableError(
                "e2b: CPU and memory come from the sandbox template, not the profile's "
                "resources: build a template (e2b template build --cpu-count N "
                "--memory-mb M) and set provider_options.template")
        ttl_s = ttl_minutes_default(env) * 60
        timeout_s = int(opts.get("timeout_s") or min(ttl_s, _HOBBY_MAX_TIMEOUT_S))
        return cls(
            template=str(opts.get("template") or env.get("MO_E2B_TEMPLATE") or ""),
            timeout_s=timeout_s,
            domain=str(opts.get("domain") or env.get("E2B_DOMAIN") or ""),
            env={str(k): str(v) for k, v in (getattr(profile, "env", None) or {}).items()},
            boot=BootOptions.from_profile(profile, opts),
        )


# --- SDK seams ------------------------------------------------------------------


def _sdk() -> Any:
    try:
        import e2b  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderUnavailableError(
            "e2b: the E2B SDK is not installed — pip install 'mini-ork[e2b]'") from exc
    return e2b


def _api(env: Mapping[str, str], opts: E2BOptions | None = None) -> dict[str, Any]:
    """Connection kwargs every SDK call takes (key through the vault chain)."""
    params: dict[str, Any] = {"api_key": resolve_provider_secret("E2B_API_KEY", env,
                                                                  provider=NAME)}
    domain = (opts.domain if opts else "") or (env.get("E2B_DOMAIN") or "").strip()
    if domain:
        params["domain"] = domain
    return params


def _is(exc: BaseException, *names: str) -> bool:
    return any(cls.__name__ in names for cls in type(exc).__mro__)


def _unavailable(action: str, exc: BaseException) -> ProviderUnavailableError:
    return ProviderUnavailableError(f"e2b: {action} failed: {type(exc).__name__}: {exc}")


# --- the adapter ------------------------------------------------------------------


class _E2BSandbox:
    """An E2B sandbox as :class:`~.sandbox_node.SandboxHandle`."""

    def __init__(self, sandbox: Any, labels: Mapping[str, str] | None = None) -> None:
        self._sb = sandbox
        self.id = str(sandbox.sandbox_id)
        self.name = self.id
        self.labels = dict(labels or {})
        self._home = ""

    def alive(self) -> bool:
        try:
            return bool(self._sb.is_running())
        except Exception:  # noqa: BLE001
            return False

    def home(self) -> str:
        if not self._home:
            rc, out = self.run("printf %s \"$HOME\"", timeout=30)
            if rc != 0 or not out.strip():
                raise ProviderUnavailableError(f"e2b: cannot read $HOME in {self.id}: {out}")
            self._home = out.strip()
        return self._home

    def upload(self, data: bytes, path: str) -> None:
        try:
            self._sb.files.write(path, data)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(f"upload {path}", exc) from exc

    def download(self, path: str) -> bytes:
        return bytes(self._sb.files.read(path, format="bytes"))

    def run(self, command: str, *, timeout: int) -> tuple[int, str]:
        try:
            result = self._sb.commands.run(command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            # The SDK raises on a non-zero exit instead of returning it.
            if _is(exc, "CommandExitException"):
                return (int(getattr(exc, "exit_code", 1) or 1),
                        f"{getattr(exc, 'stdout', '') or ''}{getattr(exc, 'stderr', '') or ''}")
            raise _unavailable("exec", exc) from exc
        return int(result.exit_code), f"{result.stdout or ''}{result.stderr or ''}"

    def start_background(self, command: str) -> None:
        try:
            self._sb.commands.run(command, background=True)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("node-agent start", exc) from exc

    def endpoint(self, port: int) -> tuple[str, dict[str, str]]:
        token = getattr(self._sb, "traffic_access_token", None)
        if not token:
            raise ProviderUnavailableError(
                f"e2b: sandbox {self.id} has no traffic access token; it must be created "
                "with public traffic off")
        return f"https://{self._sb.get_host(port)}", {TRAFFIC_TOKEN_HEADER: str(token)}

    def delete(self, *, wait: bool) -> None:
        del wait  # E2B kill is synchronous
        try:
            self._sb.kill()          # False when already gone: success either way
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "NotFoundException", "SandboxNotFoundException"):
                return
            raise _unavailable(f"sandbox kill {self.id}", exc) from exc


class E2BProvider(SandboxNodeProvider):
    name = NAME

    def boot_options(self, profile: Any, env: Mapping[str, str]) -> BootOptions:
        return E2BOptions.resolve(profile, env).boot

    def spawn_sandbox(self, *, run_id: str, profile: Any,
                      env: Mapping[str, str]) -> _E2BSandbox:
        opts = E2BOptions.resolve(profile, env)
        labels = run_labels(NAME, run_id)
        kwargs: dict[str, Any] = {
            "timeout": opts.timeout_s,
            "metadata": labels,
            "envs": dict(opts.env),
            # Public traffic OFF: the port proxy then demands the per-sandbox
            # traffic token, a second lock in front of the node-agent bearer.
            "network": {"allow_public_traffic": False},
            **_api(env, opts),
        }
        if opts.template:
            kwargs["template"] = opts.template
        try:
            return _E2BSandbox(_sdk().Sandbox.create(**kwargs), labels)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("sandbox create", exc) from exc

    def _query(self, env: Mapping[str, str], metadata: dict[str, str]) -> list[Any]:
        sdk = _sdk()
        pages = sdk.Sandbox.list(query=sdk.SandboxQuery(metadata=metadata), **_api(env))
        items: list[Any] = []
        while True:
            items.extend(pages.next_items())
            if not getattr(pages, "has_next", False):
                return items

    def find_run_sandbox(self, *, run_id: str, env: Mapping[str, str]) -> _E2BSandbox | None:
        try:
            found = self._query(env, {LABEL_PROVIDER: NAME, LABEL_RUN: run_id[:63]})
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("sandbox list", exc) from exc
        for info in found:
            sandbox = self.get_sandbox(str(info.sandbox_id), env)
            if sandbox is not None:
                return sandbox
        return None

    def get_sandbox(self, handle: str, env: Mapping[str, str]) -> _E2BSandbox | None:
        try:
            sb = _sdk().Sandbox.connect(handle, **_api(env))
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is(exc, "NotFoundException", "SandboxNotFoundException"):
                return None
            raise _unavailable(f"sandbox connect {handle}", exc) from exc
        try:
            labels = dict(getattr(sb.get_info(), "metadata", None) or {})
        except Exception:  # noqa: BLE001
            labels = {}
        return _E2BSandbox(sb, labels)

    def list_sandboxes(self, env: Mapping[str, str]) -> Iterable[Any]:
        return [_E2BListed(info, env)
                for info in self._query(env, {LABEL_SANDBOX: "1", LABEL_PROVIDER: NAME})]


class _E2BListed:
    """A listed sandbox for the TTL sweep: id + labels, deleted by connect+kill."""

    def __init__(self, info: Any, env: Mapping[str, str]) -> None:
        self.id = str(info.sandbox_id)
        self.name = self.id
        self.labels = dict(getattr(info, "metadata", None) or {})
        self._env = env

    def delete(self, *, wait: bool) -> None:
        sandbox = E2BProvider().get_sandbox(self.id, self._env)
        if sandbox is not None:
            sandbox.delete(wait=wait)


register_node_provider(NAME, E2BProvider)
